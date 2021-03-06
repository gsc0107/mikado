import os
import sys
import tempfile
import gc
from .checking import create_transcript, CheckingProcess
from .annotation_parser import AnnotationParser, load_from_gtf, load_from_gff
import operator
import collections
import io
from .. import exceptions
import logging.handlers
import random
import functools
import multiprocessing
import multiprocessing.connection
import multiprocessing.sharedctypes
import pyfaidx
import shelve
import logging
from ..utilities import path_join, to_gff, merge_partial
from collections import Counter

__author__ = 'Luca Venturini'


def store_transcripts(shelf_stacks, logger, min_length=0):

    """
    Function that analyses the exon lines from the original file
    and organises the data into a proper dictionary.
    :param shelf_stacks: dictionary containing the name and the handles of the shelf DBs
    :type shelf_stacks: dict

    :param logger: logger instance.
    :type logger: logging.Logger

    :param min_length: minimal length of the transcript.
    If it is not met, the transcript will be discarded.
    :type min_length: int

    :return: transcripts: dictionary which will be the final output
    :rtype: transcripts
    """

    transcripts = collections.defaultdict(dict)

    for shelf_name, exon_lines in shelf_stacks.items():
        for tid in exon_lines:
            if "features" not in exon_lines[tid]:
                raise KeyError("{0}: {1}\n{2}".format(tid, "features", exon_lines[tid]))

            if ("exon" not in exon_lines[tid]["features"] or
                    len(exon_lines[tid]["features"]["exon"]) == 0):
                logger.warning("No valid exon feature for %s, continuing", tid)

            chrom = exon_lines[tid]["chrom"]
            start = min((_[0] for _ in exon_lines[tid]["features"]["exon"]))
            end = max((_[1] for _ in exon_lines[tid]["features"]["exon"]))
            tlength = sum(exon[1] + 1 - exon[0] for exon in exon_lines[tid]["features"]["exon"])
            # Discard transcript under a certain size
            if tlength < min_length:
                logger.debug("Discarding %s because its size (%d) is under the minimum of %d",
                             tid, tlength, min_length)
                continue

            if (start, end) not in transcripts[chrom]:
                transcripts[chrom][(start, end)] = []
            transcripts[chrom][(start, end)].append((tid, shelf_name))

    # logger.info("Starting to sort %d transcripts", len(exon_lines))
    # keys = []
    # counter = 0

    for chrom in sorted(transcripts.keys()):

        logger.debug("Starting with %s (%d positions)",
                     chrom,
                     len(transcripts[chrom]))

        for key in sorted(transcripts[chrom].keys(),
                          key=operator.itemgetter(0, 1)):
            tids = transcripts[chrom][key]
            if len(tids) > 1:
                exons = collections.defaultdict(list)
                for tid, shelf in tids:
                    exon_set = tuple(sorted(
                        [(exon[0], exon[1], shelf_stacks[shelf][tid]["strand"]) for exon in
                         shelf_stacks[shelf][tid]["features"]["exon"]],
                        key=operator.itemgetter(0, 1)))
                    exons[exon_set].append((tid, shelf))
                tids = []
                logger.debug("%d intron chains for pos %s",
                             len(exons), "{}:{}-{}".format(chrom, key[0], key[1]))
                for tid_list in exons.values():
                    if len(tid_list) > 1:
                        logger.debug("The following transcripts are redundant: %s",
                                     ",".join([_[0] for _ in tid_list]))
                        to_keep = random.choice(tid_list)
                        logger.debug("Keeping only %s out of the list",
                                     to_keep)
                        tids.append(to_keep)
                    else:
                        tids.extend(tid_list)

            # seq = chrom_seq[key[0]-1:key[1]]
            for tid in tids:
                # counter += 1
                yield [tid, chrom, key]
            # keys.extend([tid, seq] for tid in tids)

    # logger.info("Finished to sort %d transcripts, %d remain", len(exon_lines), counter)

    # return keys


def perform_check(keys, shelve_stacks, args, logger):

    """
    This is the most important method. After preparing the data structure,
    this function creates the real transcript instances and checks that
    they are correct when looking at the underlying genome sequence.
    This is also the point at which we start using multithreading, if
    so requested.
    :param keys: sorted list of [tid, sequence]
    :param shelve_stacks: dictionary containing the name and the handles of the shelf DBs
    :param args: the namespace
    :param logger: logger
    :return:
    """

    counter = 0

    # FASTA extraction *has* to be done at the main process level, it's too slow
    # to create an index in each process.

    if args.json_conf["prepare"]["single"] is True or args.procs == 1:

        # Use functools to pre-configure the function
        # with all necessary arguments aside for the lines
        partial_checker = functools.partial(
            create_transcript,
            lenient=args.json_conf["prepare"]["lenient"],
            # strand_specific=args.json_conf["prepare"]["strand_specific"],
            canonical_splices=args.json_conf["prepare"]["canonical"],
            logger=logger)

        for tid, chrom, key in keys:
            tid, shelf_name = tid
            transcript_object = partial_checker(
                shelve_stacks[shelf_name][tid],
                str(args.json_conf["reference"]["genome"][chrom][key[0]-1:key[1]]),
                key[0], key[1],
                strand_specific=shelve_stacks[shelf_name][tid]["strand_specific"])
            if transcript_object is None:
                continue
            counter += 1
            if counter >= 10**4 and counter % (10**4) == 0:
                logger.info("Retrieved %d transcript positions", counter)
            elif counter >= 10**3 and counter % (10**3) == 0:
                logger.debug("Retrieved %d transcript positions", counter)
            print(transcript_object.format("gtf"),
                  file=args.json_conf["prepare"]["files"]["out"])
            print(transcript_object.fasta,
                  file=args.json_conf["prepare"]["files"]["out_fasta"])
    else:
        # pylint: disable=no-member

        submission_queue = multiprocessing.Queue(-1)

        working_processes = [CheckingProcess(
            submission_queue,
            args.logging_queue,
            args.json_conf["reference"]["genome"].filename,
            _ + 1,
            os.path.basename(args.json_conf["prepare"]["files"]["out_fasta"].name),
            os.path.basename(args.json_conf["prepare"]["files"]["out"].name),
            args.tempdir.name,
            lenient=args.json_conf["prepare"]["lenient"],
            # strand_specific=args.json_conf["prepare"]["strand_specific"],
            canonical_splices=args.json_conf["prepare"]["canonical"],
            log_level=args.level) for _ in range(args.procs)]

        [_.start() for _ in working_processes]

        for counter, keys in enumerate(keys):
            tid, chrom, (pos) = keys
            tid, shelf_name = tid
            submission_queue.put((shelve_stacks[shelf_name][tid], pos[0], pos[1], counter + 1))

        submission_queue.put(tuple(["EXIT"]*4))

        [_.join() for _ in working_processes]

        partial_gtf = [os.path.join(args.tempdir.name,
                                    "{0}-{1}".format(
                                        os.path.basename(args.json_conf["prepare"]["files"]["out"].name),
                                        _ + 1)) for _ in range(args.procs)]
        merge_partial(partial_gtf, args.json_conf["prepare"]["files"]["out"])

        partial_fasta = [os.path.join(
            args.tempdir.name,
            "{0}-{1}".format(os.path.basename(args.json_conf["prepare"]["files"]["out_fasta"].name), _ + 1))
                         for _ in range(args.procs)]
        merge_partial(partial_fasta, args.json_conf["prepare"]["files"]["out_fasta"])

    args.json_conf["prepare"]["files"]["out_fasta"].close()
    args.json_conf["prepare"]["files"]["out"].close()

    logger.setLevel(logging.INFO)
    # logger.info("Finished to analyse %d transcripts (%d retained)",
    #             len(exon_lines), counter)
    logger.setLevel(args.level)
    return


def load_exon_lines(args, shelve_names, logger):

    """This function loads all exon lines from the GFF inputs into a
     defaultdict instance.
    :param args: the Namespace from the command line.
    :param shelve_names: list of names of the shelf DB files.
    :param logger: the logger instance.
    :type logger: logging.Logger
    :return: exon_lines
    :rtype: collections.defaultdict[list]
    """

    threads = min([len(args.json_conf["prepare"]["files"]["gff"]), args.procs])
    strip_cds = args.json_conf["prepare"]["strip_cds"]

    if args.json_conf["prepare"]["single"] is True or threads == 1:

        logger.info("Starting to load lines from %d files (single-threaded)",
                    len(args.json_conf["prepare"]["files"]["gff"]))
        previous_file_ids = collections.defaultdict(set)
        for new_shelf, label, strand_specific, gff_name in zip(
                shelve_names,
                args.json_conf["prepare"]["files"]["labels"],
                args.json_conf["prepare"]["files"]["strand_specific_assemblies"],
                args.json_conf["prepare"]["files"]["gff"]):
            logger.info("Starting with %s", gff_name)
            gff_handle = to_gff(gff_name)
            found_ids = set.union(set(), *previous_file_ids.values())
            if gff_handle.__annot_type__ == "gff3":
                new_ids = load_from_gff(new_shelf,
                                        gff_handle,
                                        label,
                                        found_ids,
                                        logger,
                                        strip_cds=strip_cds,
                                        strand_specific=strand_specific)
            else:
                new_ids = load_from_gtf(new_shelf,
                                        gff_handle,
                                        label,
                                        found_ids,
                                        logger,
                                        strip_cds=strip_cds,
                                        strand_specific=strand_specific)

            previous_file_ids[gff_handle.name] = new_ids
    else:
        logger.info("Starting to load lines from %d files (using %d processes)",
                    len(args.json_conf["prepare"]["files"]["gff"]), threads)
        submission_queue = multiprocessing.Queue(-1)

        working_processes = [AnnotationParser(
            submission_queue,
            args.logging_queue,
            _ + 1,
            log_level=args.level,
            strip_cds=strip_cds) for _ in range(threads)]

        [_.start() for _ in working_processes]
        for new_shelf, label, strand_specific, gff_name in zip(
                shelve_names,
                args.json_conf["prepare"]["files"]["labels"],
                args.json_conf["prepare"]["files"]["strand_specific_assemblies"],
                args.json_conf["prepare"]["files"]["gff"]):

            submission_queue.put((label, gff_name, strand_specific, new_shelf))

        submission_queue.put(("EXIT", "EXIT", "EXIT", "EXIT"))

        [_.join() for _ in working_processes]

        tid_counter = Counter()
        for shelf in shelve_names:
            with shelve.open(shelf, flag="r") as shelf_handle:
                tid_counter.update(shelf_handle.keys())
                if tid_counter.most_common()[0][1] > 1:
                    if set(args.json_conf["prepare"]["files"]["labels"]) == {""}:
                        exception = exceptions.RedundantNames(
                            """Found redundant names during multiprocessed file analysis.
                            Please repeat using distinct labels for your input files. Aborting.""")
                    else:
                        exception = exceptions.RedundantNames(
                            """Found redundant names during multiprocessed file analysis, even if
                            unique labels were provided. Please try to repeat with a different and
                            more unique set of labels. Aborting.""")
                    logger.exception(exception)
                    raise exception

        del working_processes
        gc.collect()

    logger.info("Finished loading lines from %d files",
                len(args.json_conf["prepare"]["files"]["gff"]))

    return


def prepare(args, logger):
    """Main script function.

    :param args: the ArgumentParser-derived namespace.
    :param logger: a logging instance
    :type logger: logging.Logger
    """

    if not isinstance(args.json_conf["reference"]["genome"], io.TextIOWrapper):
        if not (isinstance(args.json_conf["reference"]["genome"], str) and
                os.path.exists(args.json_conf["reference"]["genome"])):
            logger.critical("Invalid FASTA file: %s",
                            args.json_conf["reference"]["genome"])
            sys.exit(1)
        else:
            pass
    else:
        args.json_conf["reference"]["genome"].close()
        args.json_conf["reference"]["genome"] = args.json_conf["reference"]["genome"].name

    assert len(args.json_conf["prepare"]["files"]["gff"]) > 0
    assert len(args.json_conf["prepare"]["files"]["gff"]) == len(args.json_conf["prepare"]["files"]["labels"])

    if args.json_conf["prepare"]["strand_specific"] is True:
        args.json_conf["prepare"]["files"]["strand_specific_assemblies"] = [True] * len(
            args.json_conf["prepare"]["files"]["gff"])
    else:
        args.json_conf["prepare"]["files"]["strand_specific_assemblies"] = [
            (member in args.json_conf["prepare"]["files"]["strand_specific_assemblies"])
            for member in args.json_conf["prepare"]["files"]["gff"]]

    shelve_names = [path_join(args.json_conf["prepare"]["files"]["output_dir"],
                              "mikado_shelf_{}.shelf".format(str(_).zfill(5))) for _ in
                    range(len(args.json_conf["prepare"]["files"]["gff"]))]

    logger.propagate = False
    if args.json_conf["prepare"]["single"] is False and args.procs > 1:
        multiprocessing.set_start_method(args.json_conf["multiprocessing_method"],
                                         force=True)
        args.logging_queue = multiprocessing.Queue(-1)
        log_queue_handler = logging.handlers.QueueHandler(args.logging_queue)
        log_queue_handler.setLevel(logging.DEBUG)
        # logger.addHandler(log_queue_handler)
        args.tempdir = tempfile.TemporaryDirectory(dir=args.json_conf["prepare"]["files"]["output_dir"])
        args.listener = logging.handlers.QueueListener(args.logging_queue, logger)
        args.listener.propagate = False
        args.listener.start()

    args.json_conf["prepare"]["files"]["out_fasta"] = open(
        path_join(args.json_conf["prepare"]["files"]["output_dir"],
                  args.json_conf["prepare"]["files"]["out_fasta"]), 'w')
    args.json_conf["prepare"]["files"]["out"] = open(path_join(
        args.json_conf["prepare"]["files"]["output_dir"],
        args.json_conf["prepare"]["files"]["out"]), 'w')

    logger.info("Loading reference file")
    args.json_conf["reference"]["genome"] = pyfaidx.Fasta(args.json_conf["reference"]["genome"])

    logger.info("Finished loading genome file")
    logger.info("Started loading exon lines")

    try:
        load_exon_lines(args, shelve_names, logger)

        logger.info("Finished loading exon lines")

        # Prepare the sorted data structure
        sorter = functools.partial(
            store_transcripts,
            logger=logger,
            min_length=args.json_conf["prepare"]["minimum_length"]
        )

        try:
            shelf_stacks = dict((_, shelve.open(_, flag="r")) for _ in shelve_names)
        except Exception as exc:
            raise TypeError((shelve_names, exc))
        perform_check(sorter(shelf_stacks), shelf_stacks, args, logger)
    except Exception as exc:
        logger.exception(exc)
        for fn in os.listdir(args.json_conf["prepare"]["files"]["output_dir"]):
            if ("shelf" in fn and
                    (path_join(args.json_conf["prepare"]["files"]["output_dir"], fn) in shelve_names or
                     path_join(args.json_conf["prepare"]["files"]["output_dir"],
                              os.path.splitext(os.path.basename(fn))[0]) in shelve_names)):
                os.remove(path_join(args.json_conf["prepare"]["files"]["output_dir"], os.path.basename(fn)))

        logger.error("Mikado has encountered an error, exiting")
        sys.exit(1)

    # args.json_conf["prepare"]["files"]["out"].close()
    # args.json_conf["prepare"]["files"]["out_fasta"].close()

    if args.json_conf["prepare"]["single"] is False and args.procs > 1:
        args.tempdir.cleanup()
        args.listener.enqueue_sentinel()

    logger.setLevel(logging.INFO)
    for fn in os.listdir(args.json_conf["prepare"]["files"]["output_dir"]):
        if ("shelf" in fn and
                (path_join(args.json_conf["prepare"]["files"]["output_dir"], fn) in shelve_names or
                         path_join(args.json_conf["prepare"]["files"]["output_dir"],
                                   os.path.splitext(os.path.basename(fn))[0]) in shelve_names)):
            os.remove(path_join(args.json_conf["prepare"]["files"]["output_dir"], os.path.basename(fn)))
    logger.info("Finished")
    sys.exit(0)
    # for handler in logger.handlers:
    #     handler.close()
