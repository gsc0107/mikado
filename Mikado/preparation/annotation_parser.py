import multiprocessing
from ..utilities import to_gff
from ..utilities.log_utils import create_queue_logger
import logging
import logging.handlers
from .. import exceptions
from sys import intern
import shelve

__author__ = 'Luca Venturini'


class AnnotationParser(multiprocessing.Process):

    def __init__(self,
                 submission_queue,
                 logging_queue,
                 identifier,
                 log_level="WARNING",
                 strip_cds=False):

        super().__init__()
        self.submission_queue = submission_queue
        self.__strip_cds = strip_cds
        self.logging_queue = logging_queue
        self.log_level = log_level
        self.__identifier = identifier
        self.name = "AnnotationParser-{0}".format(self.identifier)
        self.logger = None
        self.handler = None
        self.logger = logging.getLogger(self.name)
        create_queue_logger(self, prefix="prepare")

        self.logger.info("Started process %s", self.name)

    def __getstate__(self):

        state = self.__dict__.copy()
        del state["logger"]
        del state["handler"]
        return state

    def __setstate__(self, state):

        self.__dict__.update(state)
        create_queue_logger(self)

    def run(self):

        # exon_lines = collections.defaultdict(dict)
        found_ids = set()
        self.logger.debug("Starting to listen to the queue")
        counter = 0
        while True:
            results = self.submission_queue.get()
            try:
                label, handle, strand_specific, shelf_name = results
            except ValueError as exc:
                raise ValueError("{}.\tValues: {}".format(exc, ", ".join([str(_) for _ in results])))
            if handle == "EXIT":
                self.submission_queue.put(("EXIT", "EXIT", "EXIT", "EXIT"))
                break
            counter += 1
            self.logger.debug("Received %s (label: %s; SS: %s, shelf_name: %s)",
                              handle,
                              label,
                              strand_specific,
                              shelf_name)
            try:
                gff_handle = to_gff(handle)
                if gff_handle.__annot_type__ == "gff3":
                    new_ids = load_from_gff(shelf_name,
                                            gff_handle,
                                            label,
                                            found_ids,
                                            self.logger,
                                            strip_cds=self.__strip_cds,
                                            strand_specific=strand_specific)
                else:
                    new_ids = load_from_gtf(shelf_name,
                                            gff_handle,
                                            label,
                                            found_ids,
                                            self.logger,
                                            strip_cds=self.__strip_cds,
                                            strand_specific=strand_specific)
                if len(new_ids) == 0:
                    raise exceptions.InvalidAssembly(
                        "No valid transcripts found in {0}{1}!".format(
                            handle, " (label: {0})".format(label) if label != "" else ""
                        ))

            except exceptions.InvalidAssembly as exc:
                self.logger.exception(exc)
                continue
            except Exception as exc:
                self.logger.exception(exc)
                raise

        self.logger.debug("Sending %s back, exiting.", counter)

    @property
    def identifier(self):
        """
        A numeric value that identifies the process uniquely.
        :return:
        """
        return self.__identifier


def __raise_redundant(row_id, name, label):

    if label == '':

        raise exceptions.RedundantNames(
            """{0} has already been found in another file but is present in {1};
               this will cause unsolvable collisions. Please rerun preparation using
               labels to tag each file.""".format(
                row_id, name
            ))
    else:
        raise exceptions.RedundantNames(
            """"{0} has already been found in another file but is present in {1};
            this will cause unsolvable collisions. This happened even if
            you specified label {2}; please change them in order to ensure that
            no collisions happen.""".format(row_id, name, label))


def __raise_invalid(row_id, name, label):

    raise exceptions.InvalidAssembly(
        """{0} is present multiple times in {1}{2}. This breaks the input parsing.
        Please ensure that the file is properly formatted, with unique IDs.""".format(
            row_id,
            name,
            "(label: {0})".format(label) if label != '' else ""))


def load_from_gff(shelf_name,
                  gff_handle,
                  label,
                  found_ids,
                  logger,
                  strip_cds=False,
                  strand_specific=False):
    """
    Method to load the exon lines from GFF3 files.
    :param shelf_name: the name of the shelf DB to use.
    :param gff_handle: The handle for the GTF to be parsed.
    :param label: label to be attached to all transcripts.
    :type label: str
    :param found_ids: set of IDs already found in other files.
    :type found_ids: set
    :param logger: a logger to be used to pass messages
    :type logger: logging.Logger
    :param strip_cds: boolean flag. If true, all CDS lines will be ignored.
    :type strip_cds: bool
    :param strand_specific: whether the assembly is strand-specific or not.
    :type strand_specific: bool
    :return:
    """

    exon_lines = dict()

    transcript2genes = dict()
    new_ids = set()

    to_ignore = set()

    for row in gff_handle:
        if row.is_transcript is True:
            if label != '':
                row.id = "{0}_{1}".format(label, row.id)
                row.source = label
            if row.id in found_ids:
                    __raise_redundant(row.id, gff_handle.name, label)
            elif row.id in exon_lines:
                # This might sometimes happen in GMAP
                logger.warning(
                    "Multiple instance of %s found, skipping any subsequent entry",
                    row.id)
                to_ignore.add(row.id)
                continue
            if row.id not in exon_lines:
                exon_lines[row.id] = dict()
            exon_lines[row.id]["source"] = row.source
            transcript2genes[row.id] = row.parent[0]
            if row.id in found_ids:
                __raise_redundant(row.id, gff_handle.name, label)
            # elif row.id in exon_lines:
            #     # This might sometimes happen in GMAP
            #     logger.warning(
            #         "Multiple instance of %s found, skipping any subsequent entry",
            #         row.id)
            #     to_ignore.add(row.id)
            #     continue
                # __raise_invalid(row.id, gff_handle.name, label)

            exon_lines[row.id]["attributes"] = row.attributes.copy()
            exon_lines[row.id]["chrom"] = row.chrom
            exon_lines[row.id]["strand"] = row.strand
            exon_lines[row.id]["tid"] = row.transcript
            exon_lines[row.id]["parent"] = row.parent
            exon_lines[row.id]["features"] = dict()
            exon_lines[row.id]["strand_specific"] = strand_specific
            continue
        elif not row.is_exon:
            continue
        elif row.is_exon is True:
            if not row.is_cds or (row.is_cds is True and strip_cds is False):
                if len(row.parent) == 0 and "match" in row.feature:
                    if label == '':
                        __tid = row.id
                    else:
                        __tid = "{0}_{1}".format(label, row.id)
                    row.parent = __tid
                    transcript2genes[__tid] = "{}_match".format(row.transcript)
                    row.feature = "exon"
                elif label != '':
                    row.transcript = ["{0}_{1}".format(label, tid) for tid in row.transcript]

                parents = row.transcript[:]
                for tid in parents:
                    if tid in found_ids:
                        __raise_redundant(tid, gff_handle.name, label)
                    elif tid in to_ignore:
                        continue
                    if tid not in exon_lines:
                        exon_lines[tid] = dict()
                        exon_lines[tid]["attributes"] = row.attributes.copy()
                        if label:
                            exon_lines[tid]["source"] = label
                        else:
                            exon_lines[tid]["source"] = row.source
                        exon_lines[tid]["chrom"] = row.chrom
                        exon_lines[tid]["strand"] = row.strand
                        exon_lines[tid]["features"] = dict()
                        exon_lines[tid]["tid"] = tid
                        exon_lines[tid]["parent"] = transcript2genes[tid]
                        exon_lines[tid]["strand_specific"] = strand_specific
                    else:
                        if "exon_number" in row.attributes:
                            del row.attributes["exon_number"]
                        if (exon_lines[tid]["chrom"] != row.chrom or
                                exon_lines[tid]["strand"] != row.strand):
                            __raise_invalid(tid, gff_handle.name, label)
                        exon_lines[tid]["attributes"].update(row.attributes)

                    if row.feature not in exon_lines[tid]["features"]:
                        exon_lines[tid]["features"][row.feature] = []
                    exon_lines[tid]["features"][row.feature].append((row.start, row.end))
                    new_ids.add(tid)
            else:
                continue
    gff_handle.close()

    with shelve.open(shelf_name, flag="n", writeback=True) as shelf:
        shelf.update(exon_lines)
    # if shelf_name is not None:
    #     with shelve.open(shelf_name, flag="n") as shelf:
    #         for tid in exon_lines:
    #             shelf[tid] = exon_lines[tid]
    #     exon_lines = collections.defaultdict(dict)

    return new_ids


def load_from_gtf(shelf_name,
                  gff_handle,
                  label,
                  found_ids,
                  logger,
                  strip_cds=False,
                  strand_specific=False):
    """
    Method to load the exon lines from GTF files.
    :param shelf_name: the name of the shelf DB to use.
    :param gff_handle: The handle for the GTF to be parsed.
    :param label: label to be attached to all transcripts.
    :type label: str
    :param found_ids: set of IDs already found in other files.
    :type found_ids: set
    :param logger: a logger to be used to pass messages
    :type logger: logging.Logger
    :param strip_cds: boolean flag. If true, all CDS lines will be ignored.
    :type strip_cds: bool
    :param strand_specific: whether the assembly is strand-specific or not.
    :type strand_specific: bool
    :return:
    """

    exon_lines = dict()

    # Reduce memory footprint
    [intern(_) for _ in ["chrom", "features", "strand", "attributes", "tid", "parent", "attributes"]]

    new_ids = set()
    to_ignore = set()
    for row in gff_handle:
        if row.is_transcript is True:
            if label != '':
                row.transcript = "{0}_{1}".format(label, row.transcript)
            if row.transcript in found_ids:
                __raise_redundant(row.transcript, gff_handle.name, label)
            if row.transcript in exon_lines:
                logger.warning(
                    "Multiple instance of %s found, skipping any subsequent entry", row.id)
                to_ignore.add(row.id)
                continue
                # __raise_invalid(row.transcript, gff_handle.name, label)
            if row.transcript not in exon_lines:
                exon_lines[row.transcript] = dict()
            if label:
                exon_lines[row.transcript]["source"] = label
            else:
                exon_lines[row.transcript]["source"] = row.source

            exon_lines[row.transcript]["features"] = dict()
            exon_lines[row.transcript]["chrom"] = row.chrom
            exon_lines[row.transcript]["strand"] = row.strand
            exon_lines[row.transcript]["attributes"] = row.attributes.copy()
            exon_lines[row.transcript]["tid"] = row.id
            exon_lines[row.transcript]["parent"] = row.gene
            exon_lines[row.transcript]["strand_specific"] = strand_specific
            if "exon_number" in exon_lines[row.transcript]["attributes"]:
                del exon_lines[row.id]["attributes"]["exon_number"]
            continue

        if row.is_exon is False or (row.is_cds is True and strip_cds is True):
            continue
        if label != '':
            row.transcript = "{0}_{1}".format(label, row.transcript)
        if row.transcript in found_ids:
            __raise_redundant(row.transcript, gff_handle.name, label)
        assert row.transcript is not None
        if row.transcript not in exon_lines:
            exon_lines[row.transcript] = dict()
            if label:
                exon_lines[row.transcript]["source"] = label
            else:
                exon_lines[row.transcript]["source"] = row.source
            exon_lines[row.transcript]["chrom"] = row.chrom
            exon_lines[row.transcript]["strand"] = row.strand
            exon_lines[row.transcript]["exon"] = []
            exon_lines[row.transcript]["attributes"] = row.attributes.copy()
            exon_lines[row.id]["tid"] = row.transcript
            exon_lines[row.id]["parent"] = row.gene
            exon_lines[row.id]["strand_specific"] = strand_specific
        else:
            if row.transcript in to_ignore:
                continue
            if "exon_number" in row.attributes:
                del row.attributes["exon_number"]
            if ("chrom" not in exon_lines[row.transcript] or
                    exon_lines[row.transcript]["chrom"] != row.chrom or
                    exon_lines[row.transcript]["strand"] != row.strand):
                __raise_invalid(row.transcript, gff_handle.name, label)
            exon_lines[row.transcript]["attributes"].update(row.attributes)
        if row.feature not in exon_lines[row.transcript]["features"]:
            exon_lines[row.transcript]["features"][row.feature] = []
        exon_lines[row.transcript]["features"][row.feature].append((row.start, row.end))
        new_ids.add(row.transcript)
    gff_handle.close()

    with shelve.open(shelf_name, flag="n", writeback=True) as shelf:
        shelf.update(exon_lines)

    return new_ids
