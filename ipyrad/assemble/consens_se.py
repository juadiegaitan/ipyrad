#!/usr/bin/env python

"call consensus base calls on single-end data"

# py2/3 compatible
from __future__ import print_function
try:
    from itertools import izip, chain
except ImportError:
    from itertools import chain
    izip = zip

import os
import time
import gzip
import glob
import shutil
import warnings
import subprocess as sps
from collections import Counter

import numpy as np
import pandas as pd
import scipy.stats
import scipy.misc

import ipyrad as ip
from .jointestimate import recal_hidepth
from .util import IPyradError, clustdealer, PRIORITY

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=FutureWarning)
    import h5py

# TODO NOTES
# - chunksizes are too big on tortas test data when ncpus=4
# - can we store alleles (and indels) in the cigarstring?

class Step5:
    "Organized Step 5 functions for all datatype and methods"

    def __init__(self, data, force, ipyclient):
        self.data = data
        self.force = force
        self.samples = self.get_subsamples()
        self.isref = bool("reference" in data.paramsdict["assembly_method"])
        self.ipyclient = ipyclient
        self.lbview = ipyclient.load_balanced_view()
        self.setup_dirs()


    def setup_dirs(self):
        "setup directories, remove old tmp files"

        # final results dir
        self.data.dirs.consens = os.path.join(
            self.data.dirs.project, 
            "{}_consens".format(self.data.name))
        if not os.path.exists(self.data.dirs.consens):
            os.mkdir(self.data.dirs.consens)

        # tmpfile dir (zap it if it exists)
        self.data.tmpdir = os.path.join(
            self.data.dirs.project, 
            "{}-tmpdir".format(self.data.name))
        if os.path.exists(self.data.tmpdir):
            shutil.rmtree(self.data.tmpdir)
        if not os.path.exists(self.data.tmpdir):
            os.mkdir(self.data.tmpdir)

        # assign output file handles for s6
        for sample in self.samples:
            if not self.isref:
                sample.files.consens = os.path.join(
                    self.data.dirs.consens, 
                    "{}.consens.gz".format(sample.name))
            else:
                sample.files.consens = os.path.join(
                    self.data.dirs.consens, 
                    "{}.consens.bam".format(sample.name))

        # set up parallel client: allow user to throttle cpus
        self.lbview = self.ipyclient.load_balanced_view()
        if self.data._ipcluster["cores"]:
            self.ncpus = self.data._ipcluster["cores"]
        else:
            self.ncpus = len(self.ipyclient.ids)


    def get_subsamples(self):
        "Apply state, ncluster, and force filters to select samples"

        # filter samples by state
        state3 = self.data.stats.index[self.data.stats.state < 4]
        state4 = self.data.stats.index[self.data.stats.state == 4]
        state5 = self.data.stats.index[self.data.stats.state > 4]

        # tell user which samples are not ready for step5
        if state3.any():
            print("skipping samples not in state==4:\n{}"
                  .format(state3.tolist()))

        if self.force:
            # run all samples above state 3
            subs = self.data.stats.index[self.data.stats.state > 3]
            subsamples = [self.data.samples[i] for i in subs]

        else:
            # tell user which samples have already completed step 5
            if state5.any():
                print("skipping samples already finished step 5:\n{}"
                      .format(state5.tolist()))

            # run all samples in state 4
            subsamples = [self.data.samples[i] for i in state4]

        # check that kept samples have clusters
        checked_samples = []
        for sample in subsamples:
            if sample.stats.clusters_hidepth:
                checked_samples.append(sample)
            else:
                print("skipping {}; no clusters found.")
        if not any(checked_samples):
            raise IPyradError("no samples ready for step 5")

        # sort samples so the largest is first
        checked_samples.sort(
            key=lambda x: x.stats.clusters_hidepth,
            reverse=True,
        )

        # if sample is already done skip
        if "hetero_est" not in self.data.stats:
            for sample in checked_samples:
                sample.stats.hetero_est = 0.001
                sample.stats.error_est = 0.0001

        if self.data._headers:
            print(u"  Mean error  [{:.5f} sd={:.5f}]".format(
                self.data.stats.error_est.mean(),
                self.data.stats.error_est.std(),
            ))
            print(u"  Mean hetero [{:.5f} sd={:.5f}]".format(
                self.data.stats.hetero_est.mean(),
                self.data.stats.hetero_est.std(),
            ))
        return checked_samples


    def run(self):
        "run the main functions on the parallel client"
        # this isn't setup yet to allow restarting if interrupted mid run
        try:
            self.remote_calculate_depths()
            self.remote_make_chunks()
            statsdicts = self.remote_process_chunks()
            self.remote_concatenate_chunks()
            self.data_store(statsdicts)
        finally:
            shutil.rmtree(self.data.tmpdir)
            self.data.save()


    def remote_calculate_depths(self):
        "checks whether mindepth has changed and calc nclusters and maxlen"
        # send jobs to be processed on engines
        start = time.time()
        printstr = ("calculating depths  ", "s5")
        jobs = {}
        maxlens = []
        for sample in self.samples:
            jobs[sample.name] = self.lbview.apply(
                recal_hidepth,
                *(self.data, sample))

        # block until finished
        while 1:
            ready = [i.ready() for i in jobs.values()]
            self.data._progressbar(len(ready), sum(ready), start, printstr)
            time.sleep(0.1)
            if len(ready) == sum(ready):
                break

        # check for failures and collect results
        print("")
        for sample in self.samples:
            if not jobs[sample.name].successful():
                raise IPyradError(jobs[sample.name].exception())
            else:
                hidepth, maxlen, _, _ = jobs[sample.name].result()
                # recal_hidepth(self.data, sample)
                # (not saved) stat values are for majrule min
                sample.stats["clusters_hidepth"] = hidepth
                sample.stats_dfs.s3["clusters_hidepth"] = hidepth
                maxlens.append(maxlen)
        
        # update hackersdict with max fragement length
        self.data._hackersonly["max_fragment_length"] = max(maxlens)


    def remote_make_chunks(self):
        "split clusters into chunks for parallel processing"

        # first progress bar
        start = time.time()
        printstr = ("chunking clusters   ", "s5")

        # send off samples to be chunked
        jobs = {}
        for sample in self.samples:
            jobs[sample.name] = self.lbview.apply(
                make_chunks,
                *(self.data, sample, len(self.ipyclient)))

        ## block until finished
        while 1:
            ready = [i.ready() for i in jobs.values()]
            self.data._progressbar(len(ready), sum(ready), start, printstr)
            time.sleep(0.1)
            if len(ready) == sum(ready):
                break

        ## check for failures
        print("")
        for sample in self.samples:
            if not jobs[sample.name].successful():
                raise IPyradError(jobs[sample.name].exception())


    def remote_process_chunks(self):
        "process the cluster chunks into arrays and consens or bam files"

        # send chunks to be processed
        start = time.time()
        jobs = {sample.name: [] for sample in self.samples}
        printstr = ("consens calling     ", "s5")

        # submit jobs
        for sample in self.samples:
            chunks = glob.glob(os.path.join(
                self.data.tmpdir,
                "{}.chunk.*".format(sample.name)))
            chunks.sort(key=lambda x: int(x.split('.')[-1]))

            # submit jobs
            for chunk in chunks:
                jobs[sample.name].append(
                    self.lbview.apply(
                        process_chunks,
                        *(self.data, sample, chunk, self.isref)))
                
        # track progress - just wait for all to finish before concat'ing
        allsyncs = list(chain(*[jobs[i] for i in jobs]))
        while 1:
            ready = [i.ready() for i in allsyncs]
            self.data._progressbar(len(ready), sum(ready), start, printstr)
            time.sleep(0.1)
            if len(ready) == sum(ready):
                break

        # check for failures
        for job in allsyncs:
            if not job.successful():
                ip.logger.error("error in step 5: %s", job.exception())
                raise IPyradError("error in process_chunks():\n{}"
                                  .format(job.exception()))

        # collect all results for a sample and store stats 
        print("")
        statsdicts = {}
        for sample in self.samples:
            statsdicts[sample.name] = [i.result() for i in jobs[sample.name]]
        return statsdicts


    def remote_concatenate_chunks(self):
        # concatenate and store catgs
        start = time.time()
        printstr = ("indexing alleles    ", "s5")

        # concat catgs for each sample
        asyncs1 = {}
        for sample in self.samples:
            asyncs1[sample.name] = self.lbview.apply(
                concat_catgs,                
                *(self.data, sample, self.isref))

        # collect all results for a sample and store stats 
        if self.isref:
            concat_job = concat_reference_consens
        else:
            concat_job = concat_denovo_consens
        asyncs2 = {}
        for sample in self.samples:
            asyncs2[sample.name] = self.lbview.apply(
                concat_job,
                *(self.data, sample))
            
        # track progress of stats storage
        alljobs = list(asyncs1.values()) + list(asyncs2.values())
        while 1:
            ready = [i.ready() for i in alljobs]
            self.data._progressbar(len(ready), sum(ready), start, printstr)
            time.sleep(0.1)
            if len(ready) == sum(ready):
                break

        # check for failures:
        print("")
        for job in alljobs:
            if not job.successful():
                ip.logger.error("error in step 5: %s", job.exception())
                raise IPyradError(job.exception())


    def data_store(self, statsdicts):
        "store assembly object stats"
        
        # store sample stats
        for sample in self.samples:
            store_sample_stats(self.data, sample, statsdicts[sample.name])

        # build Assembly stats
        self.data.stats_dfs.s5 = self.data._build_stat("s5")

        # write stats file
        self.data.stats_files.s5 = os.path.join(
            self.data.dirs.consens, 
            's5_consens_stats.txt')

        with open(self.data.stats_files.s5, 'w') as out:
            self.data.stats_dfs.s5.to_string(
                buf=out,
                formatters={
                    'clusters_total': '{:.0f}'.format,
                    'filtered_by_depth': '{:.0f}'.format,
                    'filtered_by_maxH': '{:.0f}'.format,
                    'filtered_by_maxN': '{:.0f}'.format,
                    'reads_consens': '{:.0f}'.format,
                    'nsites': '{:.0f}'.format,
                    'nhetero': '{:.0f}'.format,
                    'heterozygosity': '{:.5f}'.format
                })


def make_chunks(data, sample, ncpus):
    "split job into bits and pass to the client"

    # counter for split job submission
    num = 0

    # set optim size for chunks in N clusters. The first few chunks take longer
    # because they contain larger clusters, so we create 4X as many chunks as
    # processors so that they are split more evenly.
    optim = int(
        (sample.stats.clusters_total // ncpus) + \
        (sample.stats.clusters_total % ncpus))

    # open to clusters
    with gzip.open(sample.files.clusters, 'rb') as clusters:
        # create iterator to sample 2 lines at a time
        pairdealer = izip(*[iter(clusters)] * 2)

        # Use iterator to sample til end of cluster
        done = 0
        while not done:
            # grab optim clusters and write to file.
            done, chunk = clustdealer(pairdealer, optim)
            chunk = [i.decode() for i in chunk]

            # make file handle
            chunkhandle = os.path.join(
                data.tmpdir,
                "{}.chunk.{}.{}".format(sample.name, optim, num * optim))

            # write to file
            if chunk:
                with open(chunkhandle, 'wt') as outchunk:
                    outchunk.write("//\n//\n".join(chunk) + "//\n//\n")
                num += 1


def process_chunks(data, sample, chunkfile, isref):
    proc = Processor(data, sample, chunkfile, isref)
    proc.run()
    return proc.counters, proc.filters     


class Processor:
    def __init__(self, data, sample, chunkfile, isref):
        self.data = data
        self.sample = sample
        self.chunkfile = chunkfile
        self.isref = isref

        # prepare the processor
        self.set_params()
        self.init_counters()
        self.init_arrays()        
        self.chroms2ints()

        # run the processor
        self.run()

    def run(self):
        self.process_chunk()
        self.write_chunk()

    def set_params(self):
        # set max limits
        self.tmpnum = int(self.chunkfile.split(".")[-1])
        self.optim = int(self.chunkfile.split(".")[-2])
        self.este = self.data.stats.error_est.mean()
        self.esth = self.data.stats.hetero_est.mean()
        self.maxlen = self.data._hackersonly["max_fragment_length"]
        if 'pair' in self.data.paramsdict["datatype"]:
            self.maxhet = sum(self.data.paramsdict["max_Hs_consens"])
            self.maxn = sum(self.data.paramsdict["max_Ns_consens"])
        else:
            self.maxhet = self.data.paramsdict["max_Hs_consens"][0]
            self.maxn = self.data.paramsdict["max_Ns_consens"][0]
        # not enforced for ref
        if self.isref:
            self.maxn = int(1e6)
        
    def init_counters(self):
        # store data for stats counters.
        self.counters = {
            "name": self.tmpnum,
            "heteros": 0,
            "nsites": 0,
            "nconsens": 0,
        }

        # store data for what got filtered
        self.filters = {
            "depth": 0,
            "maxh": 0,
            "maxn": 0,
        }

        # store data for writing
        self.storeseq = {}

    def init_arrays(self):
        # local copies to use to fill the arrays
        self.catarr = np.zeros((self.optim, self.maxlen, 4), dtype=np.uint32)
        self.nallel = np.zeros((self.optim, ), dtype=np.uint8)
        self.refarr = np.zeros((self.optim, 3), dtype=np.int64)

    def chroms2ints(self):
        # if reference-mapped then parse the fai to get index number of chroms
        if self.isref:
            fai = pd.read_csv(
                self.data.paramsdict["reference_sequence"] + ".fai",
                names=['scaffold', 'size', 'sumsize', 'a', 'b'],
                sep="\t")
            self.faidict = {j: i for i, j in enumerate(fai.scaffold)}
            self.revdict = {j: i for i, j in self.faidict.items()}

    # ---------------------------------------------
    def process_chunk(self):
        # stream through the clusters
        with open(self.chunkfile, 'rb') as inclust:
            pairdealer = izip(*[iter(inclust)] * 2)
            done = 0
            while not done:
                done, chunk = clustdealer(pairdealer, 1)
                if chunk:  
                    self.parse_cluster(chunk)
                    if self.filter_mindepth():
                        self.build_consens_and_array()
                        self.get_heteros()
                        if self.filter_maxhetero():
                            if self.filter_maxN_minLen():
                                self.get_alleles()
                                self.store_data()


    def parse_cluster(self, chunk):
        # get names and seqs
        piece = chunk[0].decode().strip().split("\n")
        self.names = piece[0::2]
        self.seqs = piece[1::2]

        # pull replicate read info from seqs
        self.reps = [int(n.split(";")[-2][5:]) for n in self.names]

        # ref positions
        self.ref_position = (-1, 0, 0)
        if self.isref:
            # parse position from name string
            rname = self.names[0].rsplit(";", 2)[0]
            chrom, posish = rname.rsplit(":")
            pos0, pos1 = posish.split("-")
            # pull idx from .fai reference dict
            chromint = self.faidict[chrom] + 1
            self.ref_position = (int(chromint), int(pos0), int(pos1))

    def filter_mindepth(self):
        # exit if nreps is less than mindepth setting
        if not nfilter1(self.data, self.reps):
            self.filters['depth'] += 1
            return 0
        return 1

    def build_consens_and_array(self):
        # get stacks of base counts
        sseqs = [list(seq) for seq in self.seqs]
        arrayed = np.concatenate(
            [[seq] * rep for (seq, rep) in zip(sseqs, self.reps)]
        ).astype(bytes)

        # ! enforce maxlen limit !
        self.arrayed = arrayed[:, :self.maxlen]
                    
        # get unphased consens sequence from arrayed
        self.consens = base_caller(
            self.arrayed, 
            self.data.paramsdict["mindepth_majrule"], 
            self.data.paramsdict["mindepth_statistical"],
            self.esth, 
            self.este,
        )

        # trim Ns from the left and right ends
        self.consens[self.consens == b"-"] = b"N"
        trim = np.where(self.consens != b"N")[0]
        ltrim, rtrim = trim.min(), trim.max()
        self.consens = self.consens[ltrim:rtrim + 1]
        self.arrayed = self.arrayed[:, ltrim:rtrim + 1]

        # update position for trimming
        self.ref_position = (
            self.ref_position[0], 
            self.ref_position[1] + ltrim,
            self.ref_position[1] + ltrim + rtrim + 1,
            )

    def get_heteros(self):
        self.hidx = [
            i for (i, j) in enumerate(self.consens) if 
            j.decode() in list("RKSYWM")]
        self.nheteros = len(self.hidx)

    def filter_maxhetero(self):
        if not nfilter2(self.nheteros, self.maxhet):
            self.filters['maxh'] += 1
            return 0
        return 1

    def filter_maxN_minLen(self):
        if not nfilter3(self.consens, self.maxn):
            self.filters['maxn'] += 1
            return 0
        return 1

    def get_alleles(self):
        # if less than two Hs then there is only one allele
        if len(self.hidx) < 2:
            self.nalleles = 1
        else:
            # array of hetero sites
            harray = self.arrayed[:, self.hidx]
            # remove reads with - or N at variable site
            harray = harray[~np.any(harray == b"-", axis=1)]
            harray = harray[~np.any(harray == b"N", axis=1)]
            # get counts of each allele (e.g., AT:2, CG:2)
            ccx = Counter([tuple(i) for i in harray])
            # remove low freq alleles if more than 2, since they may reflect
            # seq errors at hetero sites, making a third allele, or a new
            # allelic combination that is not real.
            if len(ccx) > 2:
                totdepth = harray.shape[0]
                cutoff = max(1, totdepth // 10)
                alleles = [i for i in ccx if ccx[i] > cutoff]
            else:
                alleles = ccx.keys()
            self.nalleles = len(alleles)

            if self.nalleles == 2:
                try:
                    self.consens = storealleles(self.consens, self.hidx, alleles)
                except (IndexError, KeyError):
                    # the H sites do not form good alleles
                    ip.logger.info("failed at phasing loc, skipping")
                    ip.logger.info("""
                        consens %s
                        hidx %s
                        alleles %s
                        """, self.consens, self.hidx, alleles)
                    # todo return phase or no-phase and store for later
                    # ...

    def store_data(self):
        # current counter
        cidx = self.counters["nconsens"]
        self.nallel[cidx] = self.nalleles
        self.refarr[cidx] = self.ref_position
        # store a reduced array with only CATG
        catg = np.array(
            [np.sum(self.arrayed == i, axis=0) for i in [b'C', b'A', b'T', b'G']],
            dtype='uint16').T
        # do not allow ints larger than 65535 (uint16)
        self.catarr[cidx, :catg.shape[0], :] = catg

        # store the seqdata and advance counters
        self.storeseq[cidx] = b"".join(list(self.consens))
        self.counters["name"] += 1
        self.counters["nconsens"] += 1
        self.counters["heteros"] += self.nheteros

    # ----------------------------------------------
    def write_chunk(self):

        # find last empty size
        end = np.where(np.all(self.refarr == 0, axis=1))[0]
        if np.any(end):
            end = end.min()
        else:
            end = self.refarr.shape[0]

        # write final consens string chunk
        consenshandle = os.path.join(
            self.data.tmpdir,
            "{}_tmpcons.{}.{}".format(self.sample.name, end, self.tmpnum))

        # write chunk. 
        if self.storeseq:
            with open(consenshandle, 'wt') as outfile:

                # denovo just write the consens simple
                if not self.isref:
                    outfile.write(
                        "\n".join(
                            [">" + self.sample.name + "_" + str(key) + \
                            "\n" + self.storeseq[key].decode() 
                            for key in self.storeseq]))

                # reference needs to store if the read is revcomp to reference
                else:
                    outfile.write(
                        "\n".join(
                            ["{}:{}:{}-{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}"
                            .format(
                                self.sample.name,
                                self.refarr[i][0],
                                self.refarr[i][1],
                                self.refarr[i][2],
                                0,
                                self.revdict[self.refarr[i][0] - 1],
                                self.refarr[i][1],
                                0,
                                #"{}M".format(refarr[i][2] - refarr[i][1]),
                                make_allele_cigar(
                                    "".join(['.' if i.islower() else i for i 
                                             in self.storeseq[i].decode()]),
                                    ".",
                                    "S",
                                ),
                                #"{}M".format(len(self.storeseq[i].decode())),
                                "*",
                                0,
                                self.refarr[i][2] - self.refarr[i][1],
                                self.storeseq[i].decode(),
                                "*",
                                # "XT:Z:{}".format(
                                    # make_indel_cigar(storeseq[i].decode())
                                    # ),

                            ) for i in self.storeseq.keys()]
                            ))

        # store reduced size arrays with indexes matching to keep indexes
        tmp5 = consenshandle.replace("_tmpcons.", "_tmpcats.")
        with h5py.File(tmp5, 'w') as io5:
            # reduce catarr to uint16 size for easier storage
            self.catarr[self.catarr > 65535] = 65535
            self.catarr = self.catarr.astype(np.uint16)
            io5.create_dataset(name="cats", data=self.catarr[:end])
            io5.create_dataset(name="alls", data=self.nallel[:end])
            io5.create_dataset(name="chroms", data=self.refarr[:end])
        del self.catarr
        del self.nallel
        del self.refarr

        # return stats
        self.counters['nsites'] = sum([len(i) for i in self.storeseq.values()])
        del self.storeseq



# def process_chunks(data, sample, chunkfile, isref):
#     "make consensus base and allele calls"

#     # temporarily store the mean estimates to Assembly
#     este = data.stats.error_est.mean()
#     esth = data.stats.hetero_est.mean()

#     # get index number from tmp file name
#     tmpnum = int(chunkfile.split(".")[-1])
#     optim = int(chunkfile.split(".")[-2])

#     # prepare data for reading and use global maxfraglen
#     clusters = open(chunkfile, 'rb')
#     pairdealer = izip(*[iter(clusters)] * 2)
#     maxlen = data._hackersonly["max_fragment_length"]

#     # local copies to use to fill the arrays
#     catarr = np.zeros((optim, maxlen, 4), dtype=np.uint32)
#     nallel = np.zeros((optim, ), dtype=np.uint8)
#     refarr = np.zeros((optim, 3), dtype=np.int64)

#     # if reference-mapped then parse the fai to get index number of chroms
#     if isref:
#         fai = pd.read_csv(
#             data.paramsdict["reference_sequence"] + ".fai",
#             names=['scaffold', 'size', 'sumsize', 'a', 'b'],
#             sep="\t")
#         faidict = {j: i for i, j in enumerate(fai.scaffold)}
#         revdict = {j: i for i, j in faidict.items()}

#     # store data for stats counters.
#     counters = {
#         "name": tmpnum,
#         "heteros": 0,
#         "nsites": 0,
#         "nconsens": 0,
#     }

#     # store data for what got filtered
#     filters = {
#         "depth": 0,
#         "maxh": 0,
#         "maxn": 0,
#     }

#     # store data for writing
#     storeseq = {}

#     ## set max limits
#     if 'pair' in data.paramsdict["datatype"]:
#         maxhet = sum(data.paramsdict["max_Hs_consens"])
#         maxn = sum(data.paramsdict["max_Ns_consens"])
#     else:
#         maxhet = data.paramsdict["max_Hs_consens"][0]
#         maxn = data.paramsdict["max_Ns_consens"][0]

#     # load the refmap dictionary if refmapping
#     done = 0
#     while not done:
#         done, chunk = clustdealer(pairdealer, 1)
#         if chunk:            
#             # get names and seqs
#             piece = chunk[0].decode().strip().split("\n")
#             names = piece[0::2]
#             seqs = piece[1::2]

#             # pull replicate read info from seqs
#             reps = [int(sname.split(";")[-2][5:]) for sname in names]

#             # IF this is a reference mapped read store the chrom and pos info
#             # -1 defaults to indicating an anonymous locus, since we are using
#             # the faidict as 0 indexed. If chrompos fails it defaults to -1
#             # this is used for the 'denovo + reference' method.
#             ref_position = (-1, 0, 0)
#             if isref:
#                 # parse position from name string
#                 rname = names[0].rsplit(";", 2)[0]
#                 chrom, posish = rname.rsplit(":")
#                 pos0, pos1 = posish.split("-")
                
#                 # pull idx from .fai reference dict
#                 chromint = faidict[chrom] + 1
#                 ref_position = (int(chromint), int(pos0), int(pos1))

#             # apply filters and fill arrays
#             if nfilter1(data, reps):

#                 # get stacks of base counts
#                 sseqs = [list(seq) for seq in seqs]
#                 arrayed = np.concatenate(
#                     [[seq] * rep for seq, rep in zip(sseqs, reps)]
#                 ).astype(bytes)
#                 arrayed = arrayed[:, :maxlen]
                
#                 # get consens call for each site, applies paralog-x-site filter
#                 consens = base_caller(
#                     arrayed, 
#                     data.paramsdict["mindepth_majrule"], 
#                     data.paramsdict["mindepth_statistical"],
#                     esth, 
#                     este,
#                 )

#                 # trim Ns from the left and right ends
#                 trim = np.where(consens != "N")
#                 ltrim, rtrim = trim.min(), trim.max()
#                 consens = consens[ltrim:rtrim + 1]

#                 # update position for trimming
#                 pos0 += ltrim
#                 pos1 = rtrim + 1

#                 # apply a filter to remove low coverage sites/Ns that
#                 # are likely sequence repeat errors. This is only applied to
#                 # clusters that already passed the read-depth filter (1)
#                 # and is not applied to ref aligned data which is expected to
#                 # have internal spacers that we leave in place here. 
#                 if b"N" in consens:
#                     if not isref:
#                         try:
#                             consens, arrayed = removerepeats(consens, arrayed)

#                         except ValueError:
#                             ip.logger.info(
#                                 "Caught bad chunk w/ all Ns. Skip it.")
#                             continue

#                 # get hetero sites
#                 hidx = [
#                     i for (i, j) in enumerate(consens) if j.decode() 
#                     in list("RKSYWM")]
#                 nheteros = len(hidx)

#                 # filter for max number of hetero sites
#                 if nfilter2(nheteros, maxhet):
#                     # filter for maxN, & minlen
#                     if nfilter3(consens, maxn):
#                         # counter right now
#                         current = counters["nconsens"]
#                         # get N alleles and get lower case in consens
#                         consens, nhaps = nfilter4(consens, hidx, arrayed)
#                         # store the number of alleles observed
#                         nallel[current] = nhaps
#                         # store a reduced array with only CATG
#                         catg = np.array(
#                             [np.sum(arrayed == i, axis=0)
#                                 for i in [b'C', b'A', b'T', b'G']],
#                             dtype='uint32').T
#                         catarr[current, :catg.shape[0], :] = catg
#                         refarr[current] = ref_position

#                         # store the seqdata for tmpchunk
#                         # storeseq[counters["name"]] = b"".join(list(consens))
#                         storeseq[current] = b"".join(list(consens))
#                         counters["name"] += 1
#                         counters["nconsens"] += 1
#                         counters["heteros"] += nheteros
#                     else:
#                         filters['maxn'] += 1
#                 else:
#                     filters['maxh'] += 1
#             else:
#                 filters['depth'] += 1

#     # close infile io
#     clusters.close()

#     # find last empty size
#     end = np.where(np.all(refarr == 0, axis=1))[0]
#     if np.any(end):
#         end = end.min()
#     else:
#         end = refarr.shape[0]

#     # write final consens string chunk
#     consenshandle = os.path.join(
#         data.tmpdir,
#         "{}_tmpcons.{}.{}".format(sample.name, end, tmpnum))

#     # write chunk. 
#     if storeseq:
#         with open(consenshandle, 'wt') as outfile:

#             # denovo just write the consens simple
#             if not isref:
#                 outfile.write(
#                     "\n".join([">" + sample.name + "_" + str(key) + \
#                     "\n" + storeseq[key].decode() for key in storeseq]))

#             # reference needs to store if the read is revcomp to reference
#             else:
#                 outfile.write(
#                     "\n".join(
#                         ["{}:{}:{}-{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}"
#                         .format(
#                             sample.name,
#                             refarr[i][0],
#                             refarr[i][1],
#                             refarr[i][2],
#                             0,
#                             revdict[refarr[i][0] - 1],
#                             refarr[i][1],
#                             0,

#                             # why doesn't this line up with +2 ?
#                             # make_indel_cigar(storeseq[i].decode()),
#                             #"{}M".format(refarr[i][2] - refarr[i][1]),
#                             "{}M".format(len(storeseq[i].decode())),
#                             "*",
#                             0,

#                             ## + 2 here
#                             #refarr[i][2] - refarr[i][1],
#                             len(storeseq[i].decode()),
#                             storeseq[i].decode(),
#                             "*",
#                             # "XT:Z:{}".format(
#                                 # make_indel_cigar(storeseq[i].decode())
#                                 # ),

#                         ) for i in storeseq.keys()]
#                         ))

#     # store reduced size arrays with indexes matching to keep indexes
#     tmp5 = consenshandle.replace("_tmpcons.", "_tmpcats.")
#     with h5py.File(tmp5, 'w') as io5:
#         io5.create_dataset(name="cats", data=catarr[:end])
#         io5.create_dataset(name="alls", data=nallel[:end])
#         io5.create_dataset(name="chroms", data=refarr[:end])
#     del catarr
#     del nallel
#     del refarr

#     # return stats
#     counters['nsites'] = sum([len(i) for i in storeseq.values()])
#     del storeseq
#     return counters, filters


def concat_catgs(data, sample, isref):
    "concat catgs into a single sample catg and remove tmp files"

    # collect tmpcat files
    tmpcats = glob.glob(os.path.join(
        data.tmpdir,
        "{}_tmpcats.*".format(sample.name)))
    tmpcats.sort(key=lambda x: int(x.split(".")[-1]))

    # get full nrows of the new h5 from the tmpcat filenames
    nrows = sum([int(i.rsplit(".", 2)[-2]) for i in tmpcats])

    # get shape info from the first cat, (optim, maxlen, 4)
    optim = min(nrows, 5000)
    with h5py.File(tmpcats[0], 'r') as io5:
        _, maxlen, _ = io5['cats'].shape

    # file handle for concat'd catg array
    outcatg = os.path.join(
        data.dirs.consens,
        "{}.catg.hdf5".format(sample.name))

    # fill in the chunk array
    with h5py.File(outcatg, 'w') as ioh5:
        dcat = ioh5.create_dataset(
            name="catg",
            shape=(nrows, maxlen, 4),
            dtype=np.uint32,
            chunks=(optim, maxlen, 4),
            compression="gzip")
        dall = ioh5.create_dataset(
            name="nalleles", 
            shape=(nrows, ),
            dtype=np.uint8,
            chunks=(optim, ),
            compression="gzip")
        
        # only create chrom for reference-aligned data
        if isref:
            dchrom = ioh5.create_dataset(
                name="chroms",
                shape=(nrows, 3),
                dtype=np.int64,
                chunks=(optim, 3),
                compression="gzip")

        # Combine all those tmp cats into the big cat
        start = 0
        for icat in tmpcats:
            addon = int(icat.rsplit(".", 2)[-2])
            end = start + addon
            io5 = h5py.File(icat, 'r')
            dcat[start:end] = io5['cats'][:]
            dall[start:end] = io5['alls'][:]
            if isref:
                dchrom[start:end] = io5['chroms'][:]
            start = end
            io5.close()
            os.remove(icat)


def concat_denovo_consens(data, sample):
    "concatenate consens bits into fasta file for denovo assemblies"

    # collect consens chunk files
    combs1 = glob.glob(os.path.join(
        data.tmpdir,
        "{}_tmpcons.*".format(sample.name)))
    combs1.sort(key=lambda x: int(x.split(".")[-1]))

    # write to the file
    with gzip.open(sample.files.consens, 'wt') as out:
        for fname in combs1:
            with open(fname) as infile:
                out.write(infile.read() + "\n")
            os.remove(fname)


def concat_reference_consens(data, sample):
    "concatenates consens bits into SAM for reference assemblies"

    # write sequences to SAM file
    combs1 = glob.glob(os.path.join(
        data.tmpdir,
        "{}_tmpcons.*".format(sample.name)))
    combs1.sort(key=lambda x: int(x.split(".")[-1]))

    # open sam handle for writing to bam
    samfile = sample.files.consens.replace(".bam", ".sam")    
    with open(samfile, 'w') as outf:

        # parse fai file for writing headers
        fai = "{}.fai".format(data.paramsdict["reference_sequence"])
        fad = pd.read_csv(fai, sep="\t", names=["SN", "LN", "POS", "N1", "N2"])
        headers = ["@HD\tVN:1.0\tSO:coordinate"]
        headers += [
            "@SQ\tSN:{}\tLN:{}".format(i, j)
            for (i, j) in zip(fad["SN"], fad["LN"])
        ]
        outf.write("\n".join(headers) + "\n")

        # write to file with sample names imputed to line up with catg array
        counter = 0
        for fname in combs1:
            with open(fname) as infile:
                # impute catg ordered seqnames 
                data = infile.readlines()
                fdata = []
                for line in data:
                    name, chrom, rest = line.rsplit(":", 2)
                    fdata.append(
                        "{}_{}:{}:{}".format(name, counter, chrom, rest)
                        )
                    counter += 1
                outf.write("".join(fdata) + "\n")

    # convert to bam
    cmd = [ip.bins.samtools, "view", "-Sb", samfile]
    with open(sample.files.consens, 'w') as outbam:
        proc = sps.Popen(cmd, stderr=sps.PIPE, stdout=outbam)
        comm = proc.communicate()[1]
    if proc.returncode:
        raise IPyradError("error in samtools: {}".format(comm))
    else:
        pass
        #os.remove(samfile)
        #for fname in combs1:
        #    os.remove(fname)


def store_sample_stats(data, sample, statsdicts):
    "not parallel, store the sample objects stats"

    # record results
    xcounters = {
        "nconsens": 0,
        "heteros": 0,
        "nsites": 0,
    }
    xfilters = {
        "depth": 0,
        "maxh": 0,
        "maxn": 0,
    }

    # merge finished consens stats
    for counters, filters in statsdicts:
        # sum individual counters
        for key in xcounters:
            xcounters[key] += counters[key]
        for key in xfilters:
            xfilters[key] += filters[key]

    # set Sample stats_dfs values
    if int(xcounters['nsites']):
        prop = int(xcounters["heteros"]) / float(xcounters['nsites'])
    else:
        prop = 0

    # store stats attributes to the sample
    sample.stats_dfs.s5.nsites = int(xcounters["nsites"])
    sample.stats_dfs.s5.nhetero = int(xcounters["heteros"])
    sample.stats_dfs.s5.filtered_by_depth = xfilters['depth']
    sample.stats_dfs.s5.filtered_by_maxH = xfilters['maxh']
    sample.stats_dfs.s5.filtered_by_maxN = xfilters['maxn']
    sample.stats_dfs.s5.reads_consens = int(xcounters["nconsens"])
    sample.stats_dfs.s5.clusters_total = sample.stats_dfs.s3.clusters_total
    sample.stats_dfs.s5.heterozygosity = float(prop)

    # set the Sample stats summary value
    sample.stats.reads_consens = int(xcounters["nconsens"])

    # save state to Sample if successful
    if sample.stats.reads_consens:
        sample.stats.state = 5
    else:
        print("No clusters passed filtering in Sample: {}".format(sample.name))


# not currently used
def make_allele_cigar(seq, on='.', letter='S'):
    iii = seq.split(on)
    cig = ""
    lastletter = ""
    isi = 0
    for i, j in enumerate(iii):    
        if j == "":
            isi += 1
               
        # store matches
        else:
            if isi:
                # skip if first indel base
                if cig:
                    isi += 1
                cig += "{}{}".format(isi, letter)
                isi = 0
                lastletter = letter
            
            if lastletter == "M":
                cig += "{}{}".format(1, letter)
            cig += "{}M".format(len(j))
            lastletter = "M"
    if isi:
        cig += "{}{}".format(isi, letter)
    return cig


# not currently used
def make_indel_cigar(seq, on='-', letter='I'):
    iii = seq.split(on)
    cig = ""
    lastletter = ""
    isi = 0
    for i, j in enumerate(iii):    
        if j == "":
            isi += 1
               
        # store matches
        else:
            if isi:
                # skip if first indel base
                if cig:
                    isi += 1
                cig += "{}{}".format(isi, letter)
                isi = 0
                lastletter = letter
            
            if lastletter == "M":
                cig += "{}{}".format(1, letter)
                
            acig = ''.join('.' if i.islower() else i for i in j)
            mcig = make_allele_cigar(acig)
            cig += mcig
            lastletter = "M"
    if isi:
        cig += "{}{}".format(isi, letter)
    return cig


def apply_filters_and_fill_arrs():
    pass


def base_caller(arrayed, mindepth_majrule, mindepth_statistical, estH, estE):
    "call all sites in a locus array. Can't be jit'd yet b/c scipy"

    # an array to fill with consensus site calls
    cons = np.zeros(arrayed.shape[1], dtype=np.uint8)
    cons.fill(78)
    arr = arrayed.view(np.uint8)

    # iterate over columns
    for col in range(arr.shape[1]):
        # the site of focus
        carr = arr[:, col]

        # if site is all dash then fill it
        if np.all(carr == 45):
            cons[col] = 45
            
        # else mask all N and - sites for base call
        else:
            mask = carr == 45
            mask += carr == 78
            marr = carr[~mask]
            
            # skip if only empties
            if not marr.shape[0]:
                cons[col] = 78
                
            # skip if not variable
            elif np.all(marr == marr[0]):
                cons[col] = marr[0]

            # estimate variable site call
            else:
                # get allele freqs (first-most, second, third = p, q, r)
                counts = np.bincount(marr)

                pbase = np.argmax(counts)
                nump = counts[pbase]
                counts[pbase] = 0

                qbase = np.argmax(counts)
                numq = counts[qbase]
                counts[qbase] = 0

                ## based on biallelic depth
                bidepth = nump + numq
                if bidepth < mindepth_majrule:
                    cons[col] = 78

                else:
                    # if depth is too high, reduce to sampled int
                    if bidepth > 500:
                        base1 = int(500 * (nump / float(bidepth)))
                        base2 = int(500 * (numq / float(bidepth)))
                    else:
                        base1 = nump
                        base2 = numq

                    # make statistical base call
                    if bidepth >= mindepth_statistical:
                        ishet, prob = get_binom(base1, base2, estE, estH)
                        if prob < 0.95:
                            cons[col] = 78
                        else:
                            if ishet:
                                cons[col] = TRANS[(pbase, qbase)]
                            else:
                                cons[col] = pbase

                    # make majrule base call
                    else:
                        if nump == numq:
                            cons[col] = TRANS[(pbase, qbase)]
                        else:
                            cons[col] = pbase
    return cons.view("S1")



TRANS = {
    (71, 65): 82,
    (71, 84): 75,
    (71, 67): 83,
    (84, 67): 89,
    (84, 65): 87,
    (67, 65): 77,
    (65, 67): 77,
    (65, 84): 87,
    (67, 84): 89,
    (67, 71): 83,
    (84, 71): 75,
    (65, 71): 82,
}


def get_binom(base1, base2, estE, estH):
    "return probability of base call"
    prior_homo = (1. - estH) / 2.
    prior_hete = estH

    ## calculate probs
    bsum = base1 + base2
    hetprob = scipy.misc.comb(bsum, base1) / (2. ** (bsum))
    homoa = scipy.stats.binom.pmf(base2, bsum, estE)
    homob = scipy.stats.binom.pmf(base1, bsum, estE)

    ## calculate probs
    hetprob *= prior_hete
    homoa *= prior_homo
    homob *= prior_homo

    ## final
    probabilities = [homoa, homob, hetprob]
    bestprob = max(probabilities) / float(sum(probabilities))

    ## return
    if hetprob > homoa:
        return True, bestprob
    return False, bestprob



def removerepeats(consens, arrayed):
    """
    Checks for interior Ns in consensus seqs and removes those that are at
    low depth, here defined as less than 1/3 of the average depth. The prop 1/3
    is chosen so that mindepth=6 requires 2 base calls that are not in [N,-].

    Python3 notes:
    consens and arrayed are both in bytes in entry. Consens is converted to
    unicode for operations, and both are returned as bytes.
    """

    ## default trim no edges
    consens[consens == b"-"] = b"N"
    consens = b"".join(consens)

    ## split for pairs
    try:
        cons1, cons2 = consens.split(b"nnnn")
        split = consens.index(b"nnnn")
        arr1 = arrayed[:, :split]
        arr2 = arrayed[:, split + 4:]
    except ValueError:
        cons1 = consens
        cons2 = ""
        arr1 = arrayed

    ## trim from left and right of cons1
    edges = [None, None]
    lcons = len(cons1)
    cons1 = cons1.lstrip(b"N")
    edges[0] = lcons - len(cons1)

    ## trim from right if nonzero
    lcons = len(cons1)
    cons1 = cons1.rstrip(b"N")
    if lcons - len(cons1):
        edges[1] = -1 * (lcons - len(cons1))

    ## trim same from arrayed
    arr1 = arr1[:, edges[0]:edges[1]]

    ## trim from left and right of cons2 if present
    if cons2:
        ## trim from left and right of cons1
        edges = [None, None]
        lcons = len(cons2)
        cons2 = cons2.lstrip(b"N")
        edges[0] = lcons - len(cons2)

        ## trim from right if nonzero
        lcons = len(cons2)
        cons2 = cons2.rstrip(b"N")
        if lcons - len(cons2):
            edges[1] = -1 * (lcons - len(cons2))

        ## trim same from arrayed
        arr2 = arr2[:, edges[0]:edges[1]]

        ## reconstitute pairs
        consens = cons1 + b"nnnn" + cons2
        consens = np.array(list(consens), dtype=bytes)
        sep = np.array(arr1.shape[0] * [list(b"nnnn")])
        arrayed = np.hstack([arr1, sep, arr2])

    ## if single-end...
    else:
        consens = np.array(list(cons1), dtype=bytes)
        arrayed = arr1

    ## get column counts of Ns and -s
    ndepths = np.sum(arrayed == b'N', axis=0)
    idepths = np.sum(arrayed == b'-', axis=0)

    ## get proportion of bases that are N- at each site
    nons = ((ndepths + idepths) / float(arrayed.shape[0])) >= 0.75
    ## boolean of whether base was called N
    isn = consens == b"N"
    ## make ridx
    ridx = nons * isn

    ## apply filter
    consens = consens[~ridx]
    arrayed = arrayed[:, ~ridx]

    return consens, arrayed



def nfilter1(data, reps):
    "applies read depths filter"
    if sum(reps) >= data.paramsdict["mindepth_majrule"] and \
        sum(reps) <= data.paramsdict["maxdepth"]:
        return 1
    return 0



def nfilter2(nheteros, maxhet):
    "applies max heteros in a seq filter"
    if nheteros <= maxhet:
        return 1
    return 0



def nfilter3(consens, maxn):
    "applies filter for maxN and hard minlen (32)"
    # minimum length for clustering in vsearch
    if consens.size >= 32:
        if consens[consens == b"N"].size <= maxn:
            return 1
        return 0
    return 0



def nfilter4(consens, hidx, arrayed):
    "applies max haplotypes filter returns pass and consens"

    # if less than two Hs then there is only one allele
    if len(hidx) < 2:
        return consens, 1

    # store base calls for hetero sites
    harray = arrayed[:, hidx]

    # remove any reads that have N or - base calls at hetero sites
    # these cannot be used when calling alleles currently.
    harray = harray[~np.any(harray == b"-", axis=1)]
    harray = harray[~np.any(harray == b"N", axis=1)]

    # get counts of each allele (e.g., AT:2, CG:2)
    ccx = Counter([tuple(i) for i in harray])

    ## Two possibilities we would like to distinguish, but we can't. Therefore,
    ## we just throw away low depth third alleles that are within seq. error.
    ## 1) a third base came up as a sequencing error but is not a unique allele
    ## 2) a third or more unique allele is there but at low frequency

    ## remove low freq alleles if more than 2, since they may reflect
    ## sequencing errors at hetero sites, making a third allele, or a new
    ## allelic combination that is not real.
    if len(ccx) > 2:
        totdepth = harray.shape[0]
        cutoff = max(1, totdepth // 10)
        alleles = [i for i in ccx if ccx[i] > cutoff]
    else:
        alleles = ccx.keys()

    ## how many high depth alleles?
    nalleles = len(alleles)

    ## if 2 alleles then save the phase using lowercase coding. 
    # todo: store for each allele whether it is phased or not.
    if nalleles == 2:
        try:
            consens = storealleles(consens, hidx, alleles)
        except (IndexError, KeyError):
            ## the H sites do not form good alleles
            ip.logger.info("failed at phasing loc, skipping")
            ip.logger.info("""
    consens %s
    hidx %s
    alleles %s
                """, consens, hidx, alleles)
        return consens, nalleles

    ## just return the info for later filtering
    else:
        return consens, nalleles



def storealleles(consens, hidx, alleles):
    """ store phased allele data for diploids """
    ## find the first hetero site and choose the priority base
    ## example, if W: then priority base in A and not T. PRIORITY=(order: CATG)
    bigbase = PRIORITY[consens[hidx[0]]]

    ## find which allele has priority based on bigbase
    bigallele = [i for i in alleles if i[0] == bigbase][0]

    ## uplow other bases relative to this one and the priority list
    ## e.g., if there are two hetero sites (WY) and the two alleles are
    ## AT and TC, then since bigbase of (W) is A second hetero site should
    ## be stored as y, since the ordering is swapped in this case; the priority
    ## base (C versus T) is C, but C goes with the minor base at h site 1.
    #consens = list(consens)
    for hsite, pbase in zip(hidx[1:], bigallele[1:]):
        if PRIORITY[consens[hsite]] != pbase:
            consens[hsite] = consens[hsite].lower()

    ## return consens
    return consens
