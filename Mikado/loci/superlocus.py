#!/usr/bin/env python3
# coding: utf-8

"""
Superlocus module. The class here defined is the uppermost container for transcripts
and is used to define all the possible children (subloci, monoloci, loci, etc.)
"""

# Core imports
import collections
from sys import version_info
import networkx
from sqlalchemy.engine import Engine
from ..utilities import dbutils, grouper
from sqlalchemy.orm.session import sessionmaker
from sqlalchemy.sql.expression import and_
from sqlalchemy import bindparam
from sqlalchemy.ext import baked
from ..serializers.junction import Junction, Chrom
from ..serializers.blast_serializer import Hit, Query, Target
from ..serializers.orf import Orf
from .abstractlocus import Abstractlocus
from .monosublocus import Monosublocus
from .excluded import Excluded
from .transcript import Transcript
from .sublocus import Sublocus
from .monosublocusholder import MonosublocusHolder
from ..parsers.GFF import GffLine
from ..exceptions import NoJsonConfigError, NotInLocusError
if version_info.minor < 5:
    from sortedcontainers import SortedDict
else:
    from collections import OrderedDict as SortedDict

# The number of attributes is something I need
# pylint: disable=too-many-instance-attributes


class Superlocus(Abstractlocus):
    """The superlocus class is used to define overlapping regions
    on the genome, and it receives as input transcript class instances.
    """

    __name__ = "superlocus"

    bakery = baked.bakery()
    db_baked = bakery(lambda session: session.query(Chrom))
    db_baked += lambda q: q.filter(Chrom.name == bindparam("chrom_name"))

    junction_baked = bakery(lambda session: session.query(Junction))
    junction_baked += lambda q: q.filter(and_(
        Junction.chrom == bindparam("chrom"),
        Junction.junction_start >= bindparam("junctionStart"),
        Junction.junction_end <= bindparam("junctionEnd"),
        Junction.strand == bindparam("strand")
    ))

    hit_baked = bakery(lambda session: session.query(Hit))
    hit_baked += lambda q: q.filter(and_(
        Hit.query_id == bindparam("query_id"),
        Hit.evalue <= bindparam("evalue"),
        Hit.hit_number <= bindparam("hit_number")
    ))

    _complex_limit = (1000, 1000)

    # Junction.strand == bindparam("strand")))

    # ###### Special methods ############

    def __init__(self,
                 transcript_instance,
                 stranded=True,
                 json_conf=None,
                 source="",
                 logger=None):

        """

        :param transcript_instance: an instance of the Transcript class
        :type transcript_instance: Transcript
        :param stranded: boolean flag that indicates whether
        the Locus should use or ignore strand information
        :type stranded: bool
        :param json_conf: a configuration dictionary derived from JSON/YAML config files
        :type json_conf: dict
        :param source: optional source for the locus
        :type source: str
        :param logger: the logger for the class
        :type logger: logging.Logger

        The superlocus class is instantiated from a transcript_instance class,
        which it copies in its entirety.

        It will therefore have the following attributes:
        - chrom, strand, start, end
        - splices - a *set* which contains the position of each splice site
        - introns - a *set* which contains the positions of each
        *splice junction* (registered as 2-tuples)
        - transcripts - a *set* which holds the transcripts added to the superlocus

        The constructor method takes the following keyword arguments:
        - stranded    True if all transcripts inside the superlocus are required
        to be on the same strand
        - json_conf    Required. A dictionary with the coniguration necessary
        for scoring transcripts.
        - purge        Flag. If True, all loci holding only transcripts with a 0
        score will be deleted
        from further consideration.
        """

        super().__init__(source=source)
        self.approximation_level = 0
        self.stranded = stranded
        self.feature = self.__name__
        if json_conf is None or not isinstance(json_conf, dict):
            raise NoJsonConfigError("I am missing the configuration for prioritizing transcripts!")
        self.__regressor = None
        self.json_conf = json_conf
        self.purge = self.json_conf["pick"]["run_options"]["purge"]

        self.splices = set(self.splices)
        self.introns = set(self.introns)
        super().add_transcript_to_locus(transcript_instance)
        assert transcript_instance.monoexonic is True or len(self.introns) > 0
        if self.stranded is True:
            self.strand = transcript_instance.strand
        self.logger = logger

        # Flags
        self.subloci_defined = False
        self.monosubloci_defined = False
        self.loci_defined = False
        self.monosubloci_metrics_calculated = False

        # Objects used during computation
        self.subloci = []
        self.loci = SortedDict()
        self.sublocus_metrics = []
        self.monosubloci = []
        self.monoholders = []

        # Connection objects
        self.engine = self.sessionmaker = self.session = None
        # Excluded object
        self.excluded_transcripts = None
        self.__retained_sources = set()
        self.__data_loaded = False

    def __create_locus_lines(self, superlocus_line, new_id, print_cds=True):

        """
        Private method to prepare the lines for printing out loci
        into GFF/GTF files.
        """

        lines = []
        self.define_loci()
        if len(self.loci) > 0:
            source = "{0}_loci".format(self.source)
            superlocus_line.source = source
            lines.append(str(superlocus_line))
            found = dict()

            for _, locus_instance in self.loci.items():
                locus_instance.source = source
                locus_instance.parent = new_id
                if locus_instance.id in found:
                    found[locus_instance.id] += 1
                    locus_instance.counter = found[locus_instance.id]
                else:
                    found[locus_instance.id] = 0
                lines.append(locus_instance.__str__(print_cds=print_cds).rstrip())
        return lines

    def __create_monolocus_lines(self, superlocus_line, new_id, print_cds=True):

        """
        Private method to prepare the lines for printing out monosubloci
        into GFF/GTF files.
        """

        lines = []
        self.define_monosubloci()
        if len(self.monosubloci) > 0:
            source = "{0}_monosubloci".format(self.source)
            superlocus_line.source = source
            lines.append(str(superlocus_line))
            found = dict()
            for monosublocus_instance in self.monosubloci:
                monosublocus_instance.source = source
                monosublocus_instance.parent = new_id
                if monosublocus_instance.id in found:
                    found[monosublocus_instance.id] += 1
                    monosublocus_instance.counter = found[monosublocus_instance.id]
                else:
                    found[monosublocus_instance.id] = 0

                lines.append(monosublocus_instance.__str__(print_cds=print_cds).rstrip())

        return lines

    def __create_sublocus_lines(self, superlocus_line, new_id, print_cds=True):
        """
        Private method to prepare the lines for printing out subloci
        into GFF/GTF files.
        """

        source = "{0}_subloci".format(self.source)
        superlocus_line.source = source
        lines = [str(superlocus_line)]
        self.define_subloci()
        found = dict()
        for sublocus_instance in self.subloci:
            sublocus_instance.source = source
            sublocus_instance.parent = new_id
            if sublocus_instance.id in found:
                found[sublocus_instance.id] += 1
                sublocus_instance.counter = found[sublocus_instance.id]
            else:
                found[sublocus_instance.id] = 0
            lines.append(sublocus_instance.__str__(print_cds=print_cds).rstrip())
        return lines

    # This discrepancy with the base class is necessary
    # pylint: disable=arguments-differ

    def format(self, print_cds=True, level=None):

        """
        Alias for __str__.
        :param print_cds: Boolean. It indicates whether to print the CDS features or not.
        :param level: level which we wish to print for. Can be "loci", "subloci", "monosubloci"
        :return: formatted GFF strings
        """
        return self.__str__(print_cds=print_cds,
                            level=level)

    def __str__(self, level=None, print_cds=True):

        """
        :param level: level which we wish to print for. Can be "loci", "subloci", "monosubloci"
        :type level: str
        :param print_cds: flag. If set to False, only the exonic information will be printed.
        :type print_cds: bool

        This function will return the desired level of children loci.
        The keyword level accepts the following four values:
        - "None" - print whatever is available.
        - "loci" - print the final loci
        - "monosubloci" - print the monosubloci
        - "subloci" - print the subloci.

        The function will then return the desired location in GFF3-compliant format.
        """

        if abs(self.start) == float("inf"):
            return ''

        superlocus_line = GffLine('')
        superlocus_line.chrom = self.chrom
        superlocus_line.feature = self.__name__
        superlocus_line.start, \
            superlocus_line.end, \
            superlocus_line.score = self.start, self.end, "."
        superlocus_line.strand = self.strand
        superlocus_line.phase, superlocus_line.score = None, None
        new_id = "{0}_{1}".format(self.source, self.id)
        superlocus_line.id, superlocus_line.name = new_id, self.name
        if self.approximation_level > 0:
            superlocus_line.attributes["approximation_level"] = self.approximation_level
        if len(self.__retained_sources) > 0:
            superlocus_line.attributes["retained_sources"] = ",".join(
                sorted(list(self.__retained_sources))
            )

        lines = []
        if level not in (None, "loci", "subloci", "monosubloci"):
            raise ValueError("Unrecognized level: {0}".format(level))

        elif level == "loci" or (level is None and self.loci_defined is True):
            lines = self.__create_locus_lines(
                superlocus_line,
                new_id,
                print_cds=print_cds
            )
        elif level == "monosubloci" or (level is None and self.monosubloci_defined is True):
            lines = self.__create_monolocus_lines(superlocus_line,
                                                  new_id,
                                                  print_cds=print_cds)
        elif level == "subloci" or (level is None and self.monosubloci_defined is False):
            lines = self.__create_sublocus_lines(superlocus_line,
                                                 new_id,
                                                 print_cds=print_cds)
        if len(lines) > 0:
            lines.append("###")
        return "\n".join([line for line in lines if line is not None and line != ''])
    # pylint: enable=arguments-differ

    # ########### Class instance methods ############

    def split_strands(self):
        """This method will divide the superlocus on the basis of the strand.
        The rationale is to parse a GFF file without regard for the
        strand, in order to find all intersecting loci;
        and subsequently break the superlocus into the different components.
        Notice that each strand might generate more than one superlocus,
        if genes on a different strand link what are
        two different superloci.
        """

        self.logger.debug("Splitting by strand for {0}".format(self.id))
        if self.stranded is True:
            self.logger.warning("Trying to split by strand a stranded Locus, {0}!".format(self.id))
            yield self

        else:
            plus, minus, nones = [], [], []
            for cdna_id in self.transcripts:
                cdna = self.transcripts[cdna_id]
                self.logger.debug("{0}: strand {1}".format(cdna_id, cdna.strand))
                if cdna.strand == "+":
                    plus.append(cdna)
                elif cdna.strand == "-":
                    minus.append(cdna)
                elif cdna.strand is None:
                    nones.append(cdna)

            new_loci = []
            for strand in plus, minus, nones:
                if len(strand) > 0:
                    strand = sorted(strand)
                    new_locus = Superlocus(strand[0],
                                           stranded=True,
                                           json_conf=self.json_conf,
                                           source=self.source,
                                           logger=self.logger)
                    assert len(new_locus.introns) > 0 or new_locus.monoexonic is True
                    for cdna in strand[1:]:
                        if new_locus.in_locus(new_locus, cdna):
                            new_locus.add_transcript_to_locus(cdna)
                        else:
                            assert len(new_locus.introns) > 0 or new_locus.monoexonic is True
                            new_loci.append(new_locus)
                            new_locus = Superlocus(cdna,
                                                   stranded=True,
                                                   json_conf=self.json_conf,
                                                   source=self.source,
                                                   logger=self.logger)
                    assert len(new_locus.introns) > 0 or new_locus.monoexonic is True
                    new_loci.append(new_locus)

            self.logger.debug(
                "Defined %d loci by splitting by strand at %s.",
                len(new_loci), self.id)
            for new_locus in iter(sorted(new_loci)):
                if self.regressor is not None:
                    new_locus.regressor = self.regressor
                yield new_locus
        raise StopIteration

    # @profile
    def connect_to_db(self, engine):

        """
        :param engine: the connection pool
        :type engine: Engine

        This method will connect to the database using the information
        contained in the JSON configuration.
        """

        if engine is None:
            self.engine = dbutils.connect(self.json_conf)
        else:
            self.engine = engine

        self.sessionmaker = sessionmaker()
        self.sessionmaker.configure(bind=self.engine)
        self.session = self.sessionmaker()

    # @asyncio.coroutine
    def load_transcript_data(self, tid, data_dict):
        """
        :param tid: the name of the transcript to retrieve data for.
        :type tid: str

        :param data_dict: the dictionary to use for data retrieval, if specified.
        If None, a DB connection will be established to retrieve the necessary data.
        :type data_dict: (None | dict)

        This routine is used to load data for a single transcript."""

        self.logger.debug("Retrieving data for {0}".format(tid))
        self.transcripts[tid].logger = self.logger
        self.transcripts[tid].load_information_from_db(self.json_conf,
                                                       introns=self.locus_verified_introns,
                                                       session=self.session,
                                                       data_dict=data_dict)
        to_remove, to_add = False, set()

        if self.json_conf["pick"]["chimera_split"]["execute"] is True:
            if self.transcripts[tid].number_internal_orfs > 1:
                new_tr = list(self.transcripts[tid].split_by_cds())
                if len(new_tr) > 1:
                    to_add.update(new_tr)
                    to_remove = True
        del data_dict
        return to_remove, to_add
        # @profile

    def _load_introns(self, data_dict):

        """Private method to load the intron data into the locus.
        :param data_dict: Dictionary containing the preloaded data, if available.
        :return:
        """

        if len(self.introns) == 0:
            if self.monoexonic is False:
                raise ValueError("%s is multiexonic but has no introns defined!",
                                 self.id)
            self.logger.debug("No introns for %s", self.id)
            return

        self.logger.debug("Querying the DB for introns, %d total", len(self.introns))
        if data_dict is None:
            if self.json_conf["db_settings"]["db"] is None:
                return  # No data to load
            # dbquery = self.db_baked(self.session).params(chrom_name=self.chrom).all()

            ver_introns = self.engine.execute(" ".join([
                "select junction_start, junction_end, strand from junctions where",
                "chrom_id = (select chrom_id from chrom where name = \"{chrom}\")",
                "and junction_start > {start} and junction_end < {end}"]).format(
                    chrom=self.chrom, start=self.start, end=self.end
            ))
            ver_introns = dict(((junc.junction_start, junc.junction_end), junc.strand)
                                for junc in ver_introns)

            # ver_introns = set((junc.junction_start, junc.junction_end) for junc in
            #                   self.junction_baked(self.session).params(
            #                       chrom=self.chrom,
            #                       strand=self.strand,
            #                       junctionStart=self.start,
            #                       junctionEnd=self.end
            #                     ))

            self.logger.debug("Found %d verifiable introns for %s",
                              len(ver_introns), self.id)

            for intron in self.introns:
                self.logger.debug("Checking %s%s:%d-%d",
                                  self.chrom, self.strand, intron[0], intron[1])
                if (intron[0], intron[1]) in ver_introns:
                    self.logger.debug("Verified intron %s:%d-%d",
                                      self.chrom, intron[0], intron[1])
                    self.locus_verified_introns.add((intron[0],
                                                     intron[1],
                                                     ver_introns[(intron[0], intron[1])]))
        else:
            for intron in self.introns:
                self.logger.debug("Checking %s%s:%d-%d",
                                  self.chrom, self.strand, intron[0], intron[1])
                key = (self.chrom, intron[0], intron[1])
                # Ignore the strand 'cause we are loading BEFORE splitting by strand
                if key in data_dict["junctions"]:
                    self.logger.debug("Verified intron %s%s:%d-%d",
                                      self.chrom,
                                      data_dict["junctions"][key],
                                      intron[0], intron[1])
                    # Start, Stop, Strand
                    self.locus_verified_introns.add((intron[0],
                                                     intron[1],
                                                     data_dict["junctions"][key]))

    def load_all_transcript_data(self, engine=None, data_dict=None):

        """
        This method will load data into the transcripts instances,
        and perform the split_by_cds if required
        by the configuration.
        Asyncio coroutines are used to decrease runtime.

        :param engine: a connection engine
        :type engine: Engine

        :param data_dict: the dictionary to use for data retrieval, if specified.
        If None, a DB connection will be established to retrieve the necessary data.
        :type data_dict: (None | dict)

        """

        if self.__data_loaded is True:
            return

        if data_dict is None:
            self.connect_to_db(engine)

        self.logger.debug("Type of data dict: %s",
                          type(data_dict))
        if isinstance(data_dict, dict):
            self.logger.debug("Length of data dict: %s", len(data_dict))
        self._load_introns(data_dict)
        self.logger.debug("Verified %d introns for %s",
                          len(self.locus_verified_introns),
                          self.id)
        tid_keys = list(self.transcripts.keys())
        to_remove, to_add = set(), set()

        if data_dict is None:
            assert engine is not None
            self.logger.debug("Starting to load hits and orfs for %d transcripts",
                              len(tid_keys))
            data_dict = dict()
            data_dict["hits"] = collections.defaultdict(list)
            data_dict["orfs"] = collections.defaultdict(list)
            for tid_group in grouper(tid_keys, 100):
                query_ids = dict((query.query_id, query) for query in
                                 self.session.query(Query).filter(
                                     Query.query_name.in_(tid_group)))
                orfs = self.session.query(Orf).filter(Orf.query_id.in_(query_ids.keys()))
                for orf in orfs:
                    data_dict["orfs"][orf.query].append(orf.as_bed12())

                hsp_command = " ".join([
                    "select * from hsp where",
                    "hsp_evalue <= {0} and query_id in {1} order by query_id;"]).format(
                    self.json_conf["pick"]["chimera_split"]["blast_params"]["hsp_evalue"],
                    "({0})".format(", ".join([str(_) for _ in query_ids.keys()]))
                )

                hsps = dict()
                targets = set()

                for hsp in engine.execute(hsp_command):
                    if hsp.query_id not in hsps:
                        hsps[hsp.query_id] = collections.defaultdict(list)
                    hsps[hsp.query_id][hsp.target_id].append(hsp)
                    targets.add(hsp.target_id)

                hit_command = " ".join([
                    "select * from hit where evalue <= {0}",
                    "and hit_number <= {1} and query_id in {2}",
                    "order by query_id, evalue asc;"
                ]).format(
                    self.json_conf["pick"]["chimera_split"]["blast_params"]["evalue"],
                    self.json_conf["pick"]["chimera_split"]["blast_params"]["max_target_seqs"],
                    "({0})".format(", ".join([str(_) for _ in query_ids.keys()])))

                if len(targets) > 0:

                    target_ids = dict((target.target_id, target) for target in
                                      self.session.query(Target).filter(
                                          Target.target_id.in_(targets)))
                else:
                    target_ids = dict()

                current_hit = None
                for hit in engine.execute(hit_command):
                    if current_hit != hit.query_id:
                        current_hit = hit.query_id
                    current_counter = 0

                    current_counter += 1

                    my_query = query_ids[hit.query_id]
                    my_target = target_ids[hit.target_id]

                    data_dict["hits"][my_query.query_name].append(
                        Hit.as_full_dict_static(
                            hit,
                            hsps[hit.query_id][hit.target_id],
                            my_query,
                            my_target
                        )
                    )
                    # hit_counter += 1
                    # if hit_counter >= 2*10**4 and hit_counter % (2*10**4) == 0:
                    #     self.main_logger.debug("Loaded %d BLAST hits in database",
                    #                            hit_counter)

                # " ".join(
                #     ["select * from hit where evalue <= {0}",
                #      "and hit_number <= {1} and query_id in {2}",
                #      "order by query_id, evalue asc;"]).format(
                #     self.json_conf["pick"]["chimera_split"]["blast_params"]["evalue"],
                #     self.json_conf["pick"]["chimera_split"]["blast_params"]["max_target_seqs"])))
                #
                # hits = self.hit_baked(self.session).params(
                #     query_id=tid_id,
                #     evalue=self.json_conf["pick"]["chimera_split"]["blast_params"]["evalue"],
                #     hit_number=self.json_conf[
                #         "pick"]["chimera_split"]["blast_params"]["max_target_seqs"]
                #     )
                # self.logger.debug("Starting to load hits for %s",
                #                   tid)
                # for ccc, hit in enumerate(hits):
                #     data_dict["hits"][hit.query].append(hit.as_dict())

            self.logger.debug("Finished retrieving data for %d transcripts",
                              len(tid_keys))
            self.session.close()
            self.sessionmaker.close_all()

        for tid in tid_keys:
            remove_flag, new_transcripts = self.load_transcript_data(tid, data_dict)
            if remove_flag is True:
                to_remove.add(tid)
                to_add.update(new_transcripts)

        if len(to_remove) > 0:
            self.logger.debug("Adding to %s: %s",
                              self.id,
                              ",".join([tr.id for tr in to_add]))
            for transcr in to_add:
                self.add_transcript_to_locus(transcr, check_in_locus=False)
            self.logger.debug("Removing from %s: %s",
                              self.id,
                              ",".join(list(to_remove)))
            for tid in to_remove:
                self.remove_transcript_from_locus(tid)

        del data_dict

        num_coding = 0
        for tid in self.transcripts:
            if self.transcripts[tid].combined_cds_length > 0:
                num_coding += 1
            else:
                self.transcripts[tid].feature = "ncRNA"

        # num_coding = sum(1 for x in self.transcripts
        #                  if self.transcripts[x].selected_cds_length > 0)
        self.logger.debug(
            "Found %d coding transcripts out of %d in %s",
            num_coding,
            len(self.transcripts),
            self.id)

        self.session = None
        self.__data_loaded = True
        self.sessionmaker = None
        self.stranded = False

    # ##### Sublocus-related steps ######

    def __prefilter_transcripts(self):

        """Private method that will check whether there are any transcripts
        not meeting the minimum requirements specified in the configuration.
        :return:
        """

        self.excluded_transcripts = None

        not_passing = self._check_not_passing()

        if len(not_passing) > 0 and self.purge is True:
            tid = not_passing.pop()
            self.transcripts[tid].score = 0
            monosub = Monosublocus(self.transcripts[tid], logger=self.logger)
            self.excluded_transcripts = Excluded(monosub,
                                                 json_conf=self.json_conf,
                                                 logger=self.logger)
            self.excluded_transcripts.__name__ = "Excluded"
            self.remove_transcript_from_locus(tid)
            for tid in not_passing:
                self.transcripts[tid].score = 0
                self.excluded_transcripts.add_transcript_to_locus(
                    self.transcripts[tid])
                self.remove_transcript_from_locus(tid)
        return

    def __reduce_complex_loci(self, transcript_graph):

        """
        Method which checks whether a locus has too many transcripts and tries to reduce them.

        :param transcript_graph: the transcript graph to analyse for redundancies
        :return:
        """

        max_edges = max([transcript_graph.degree(node) for node in transcript_graph.nodes()])
        self.approximation_level = 0
        if len(transcript_graph) < self._complex_limit[0] and max_edges < self._complex_limit[1]:
            return transcript_graph
        self.logger.warning("Complex superlocus with %d nodes \
        with the most connected having %d edges",
                            len(transcript_graph), max_edges)

        self.approximation_level = 1
        to_remove = set()
        for tid in transcript_graph:
            current = self.transcripts[tid]
            for neighbour in transcript_graph.neighbors_iter(tid):
                if neighbour in to_remove:
                    continue
                neighbour = self.transcripts[neighbour]
                if neighbour.introns == current.introns:
                    if neighbour.start >= current.start and neighbour.end <= current.end:
                        to_remove.add(neighbour.id)
                    elif neighbour.start <= current.start and neighbour.end >= current.end:
                        to_remove.add(current.id)
                        break
        transcript_graph.remove_nodes_from(to_remove)
        max_edges = max([transcript_graph.degree(node) for node in transcript_graph.nodes()])
        if len(transcript_graph) < self._complex_limit[0] and max_edges < self._complex_limit[1]:
            self.logger.warning("Approximation level 1 for %s", self.id)
            return transcript_graph

        self.logger.warning("Still %d nodes with the most connected with %d edges \
        after approximation 1",
                            len(transcript_graph), max_edges)

        self.approximation_level = 2
        to_remove = set()
        for tid in transcript_graph:
            current = self.transcripts[tid]
            for neighbour in transcript_graph.neighbors_iter(tid):
                if neighbour in to_remove:
                    continue
                neighbour = self.transcripts[neighbour]
                inters = set.intersection(current.introns, neighbour.introns)
                if inters == current.introns:
                    neigh_first_corr = [_ for _ in neighbour.exons if
                                        _[1] == sorted(current.exons)[0][1]]
                    assert len(neigh_first_corr) == 1
                    if neigh_first_corr[0][0] > current.start:
                        continue
                    neigh_last_corr = [_ for _ in neighbour.exons if
                                       _[0] == sorted(current.exons)[-1][0]]
                    assert len(neigh_last_corr) == 1
                    if neigh_last_corr[0][1] < current.end:
                        continue
                    to_remove.add(current.id)
                    break
                elif inters == neighbour.introns:
                    curr_first_corr = [_ for _ in current.exons if
                                       _[1] == sorted(neighbour.exons)[0][1]]
                    assert len(curr_first_corr) == 1
                    if curr_first_corr[0][0] > neighbour.start:
                        continue
                    curr_last_corr = [_ for _ in current.exons if
                                      _[0] == sorted(neighbour.exons)[-1][0]]
                    assert len(curr_last_corr) == 1
                    if curr_last_corr[0][1] < neighbour.end:
                        continue
                    to_remove.add(neighbour.id)
                    continue
                else:
                    continue

                # result, _ = Assigner.compare(neighbour, current)
                # if result.j_prec == 1 and result.n_prec == 1:
                #     # Neighbour completely contained
                #     to_remove.add(neighbour.id)
                # elif result.j_recall == 1 and result.n_recall == 1:
                #     # Current completely contained
                #     to_remove.add(current)
                #     break

        transcript_graph.remove_nodes_from(to_remove)
        max_edges = max([transcript_graph.degree(node) for node in transcript_graph.nodes()])
        if len(transcript_graph) < self._complex_limit[0] and max_edges < self._complex_limit[1]:
            self.logger.warning("Approximation level 2 for %s", self.id)
            return transcript_graph
        self.logger.warning("Still %d nodes with the most connected with %d edges \
        after approximation 2",
                            len(transcript_graph), max_edges)
        # Now we are going to collapse by method
        sources = collections.defaultdict(set)
        for tid in transcript_graph:
            found = False
            for tag in self.json_conf["prepare"]["labels"]:
                if tag != '' and tag in tid:
                    sources[tag].add(tid)
                    found = True
                    break
            if found is False:
                # Fallback
                self.logger.debug("Label not found for %s", tid)
                sources[self.transcripts[tid].source].add(tid)

        new_graph = networkx.Graph()

        counter = dict()
        for source in sources:
            counter[source] = len(sources[source])
        self.logger.debug("Sources to consider: %s", counter)
        for source in sorted(sources, key=lambda key: len(sources[key])):
            self.logger.debug("Considering source %s, counter: %d",
                              source, counter[source])
            nodes = sources[source]
            acceptable = set.union(nodes, set(new_graph.nodes()))
            edges = set([edge for edge in transcript_graph.edges(
                nbunch=set.union(set(new_graph.nodes()), nodes)) if
                         edge[0] in acceptable and edge[1] in acceptable])

            counter = collections.Counter()
            for edge in edges:
                counter.update(edge)

            if len(counter.most_common()) == 0:
                edges_most_connected = 0
            else:
                edges_most_connected = counter.most_common(1)[0][1]

            if (len(acceptable) > self._complex_limit[0] or
                    edges_most_connected > self._complex_limit[1]):
                self.logger.debug("Reached the limit with source %s, %d nodes, %d max edges",
                                  source,
                                  len(acceptable),
                                  edges_most_connected)
                break
            new_graph.add_nodes_from(nodes)
            new_graph.add_edges_from(edges)
            self.logger.debug("Retained source %s", source)
            self.__retained_sources.add(source)

        self.approximation_level = 3
        self.logger.warning("Approximation level 3 for %s; retained sources: %s",
                            self.id, ",".join(self.__retained_sources))
        return new_graph

    def define_subloci(self):
        """This method will define all subloci inside the superlocus.
        Steps:
            - Call the BronKerbosch algorithm to define cliques
            - Call the "merge_cliques" algorithm the merge the cliques.
            - Create "sublocus" objects from the merged cliques
            and store them inside the instance store "subloci"
        """

        self.compile_requirements()
        if self.subloci_defined is True:
            return
        self.subloci = []

        # Check whether there is something to remove
        self.__prefilter_transcripts()

        if len(self.transcripts) == 0:
            # we have removed all transcripts from the Locus. Set the flag to True and exit.
            self.subloci_defined = True
            return

        cds_only = self.json_conf["pick"]["run_options"]["subloci_from_cds_only"]
        self.logger.debug("Calculating the transcript graph")
        transcript_graph = self.define_graph(self.transcripts,
                                             inters=self.is_intersecting,
                                             cds_only=cds_only)
        transcript_graph = self.__reduce_complex_loci(transcript_graph)
        if len(self.transcripts) > len(transcript_graph):
            self.logger.warning("Discarded %d transcripts from %s due to approximation level %d",
                                len(self.transcripts) - len(transcript_graph),
                                self.id,
                                self.approximation_level)
            for tid in set.difference(set(self.transcripts.keys()), set(transcript_graph.nodes())):
                del self.transcripts[tid]

        if len(self.transcripts) == 0:
            # we have removed all transcripts from the Locus. Set the flag to True and exit.
            self.logger.warning("Discarded all transcripts from %s", self.id)
            self.subloci_defined = True
            return

        # Reset the source with the correct value
        # for tid in self.transcripts:
        #     self.transcripts[tid].source = self.source

        self.logger.debug("Calculated the transcript graph")
        self.logger.debug("Calculating the transcript communities")
        subloci = self.find_communities(transcript_graph)
        self.logger.debug("Calculated the transcript communities")

        # Now we should define each sublocus and store it in a permanent structure of the class
        for subl in subloci:
            if len(subl) == 0:
                continue
            subl = [self.transcripts[x] for x in subl]
            subl = sorted(subl)
            new_sublocus = Sublocus(subl[0], json_conf=self.json_conf, logger=self.logger)
            if self.regressor is not None:
                new_sublocus.regressor = self.regressor
            for ttt in subl[1:]:
                try:
                    new_sublocus.add_transcript_to_locus(ttt)
                except NotInLocusError as orig_exc:
                    exc_text = """Sublocus: {0}
                    Offending transcript:{1}
                    In locus manual check: {2}
                    Original exception: {3}""".format(
                        "{0} {1}:{2}-{3} {4}".format(
                            subl[0].id, subl[0].chrom, subl[0].start,
                            subl[0].end, subl[0].exons),
                        "{0} {1}:{2}-{3} {4}".format(ttt.id, ttt.chrom,
                                                     ttt.start, ttt.end, ttt.exons),
                        "Chrom {0} Strand {1} overlap {2}".format(
                            new_sublocus.chrom == ttt.chrom,
                            "{0}/{1}/{2}".format(
                                new_sublocus.strand,
                                ttt.strand,
                                new_sublocus.strand == ttt.strand
                            ),
                            self.overlap((subl[0].start, subl[1].end),
                                         (ttt.start, ttt.end)) > 0
                        ),
                        orig_exc
                    )
                    raise NotInLocusError(exc_text)

            new_sublocus.parent = self.id
            self.subloci.append(new_sublocus)
        self.subloci = sorted(self.subloci)

        self.subloci_defined = True

    def get_sublocus_metrics(self):
        """Wrapper function to calculate the metrics inside each sublocus."""

        self.define_subloci()
        self.sublocus_metrics = []
        for sublocus_instance in self.subloci:
            sublocus_instance.get_metrics()

    def define_monosubloci(self):

        """This is a wrapper method that defines the monosubloci for each sublocus.
        """
        if self.monosubloci_defined is True:
            return

        self.logger.debug("Calculating subloci for %s, %d transcripts",
                          self.id, len(self.transcripts))
        self.define_subloci()
        self.logger.debug("Calculated subloci for %s, %d transcripts",
                          self.id, len(self.transcripts))
        self.monosubloci = []
        # Extract the relevant transcripts
        for sublocus_instance in sorted(self.subloci):
            self.excluded_transcripts = sublocus_instance.define_monosubloci(
                purge=self.purge,
                excluded=self.excluded_transcripts)
            for tid in sublocus_instance.transcripts:
                # Update the score
                self.transcripts[tid].score = sublocus_instance.transcripts[tid].score
            for monosubl in sublocus_instance.monosubloci:
                monosubl.parent = self.id
                self.monosubloci.append(monosubl)
        self.monosubloci = sorted(self.monosubloci)
        self.monosubloci_defined = True

    def print_subloci_metrics(self):
        """Wrapper method to create a csv.DictWriter instance and call
        the sublocus.print_metrics method
        on it for each sublocus."""

        self.get_sublocus_metrics()

        for slocus in self.subloci:
            for row in slocus.print_metrics():
                yield row
        if self.excluded_transcripts is not None:
            for row in self.excluded_transcripts.print_metrics():
                yield row

    def print_subloci_scores(self):
        """Wrapper method to create a csv.DictWriter instance and call the
        sublocus.print_metrics method
        on it for each sublocus."""

        self.get_sublocus_metrics()

        for slocus in self.subloci:
            for row in slocus.print_scores():
                yield row
        # if self.excluded_transcripts is not None:
        #     for row in self.excluded_transcripts.print_scores():
        #         yield row

    def print_monoholder_metrics(self):

        """Wrapper method to create a csv.DictWriter instance and call the
        MonosublocusHolder.print_metrics method
        on it."""

        self.define_monosubloci()

        # self.available_monolocus_metrics = set(self.monoholder.available_metrics)
        if len(self.monoholders) == 0:
            return
        for monoholder in self.monoholders:
            for row in monoholder.print_metrics():
                yield row

    def print_monoholder_scores(self):

        """Wrapper method to create a csv.DictWriter instance and call
        the MonosublocusHolder.print_scores method on it."""

        self.define_monosubloci()

        # self.available_monolocus_metrics = set(self.monoholder.available_metrics)
        if len(self.monoholders) == 0:
            return
        for monoholder in self.monoholders:
            for row in monoholder.print_scores():
                yield row

    def print_loci_metrics(self):

        self.define_loci()

        if len(self.loci) == 0:
            return []
        for locus in self.loci:
            for row in self.loci[locus].print_metrics():
                yield row

    def print_loci_scores(self):

        """Wrapper method to create a csv.DictWriter instance and call
        the Locus.print_scores method on it."""

        self.define_loci()

        # self.available_monolocus_metrics = set(self.monoholder.available_metrics)
        if len(self.loci) == 0:
            return
        for locus in self.loci:
            for row in self.loci[locus].print_scores():
                yield row

    def define_loci(self):
        """This is the final method in the pipeline. It creates a container
        for all the monosubloci (an instance of the class MonosublocusHolder)
        and retrieves the loci it calculates internally."""

        if self.loci_defined is True:
            return

        self.logger.debug("Calculating monosubloci for %s, %d transcripts",
                          self.id, len(self.transcripts))
        self.define_monosubloci()
        self.logger.debug("Calculated monosubloci for %s, %d transcripts",
                          self.id, len(self.transcripts))
        self.calculate_mono_metrics()

        self.loci = SortedDict()
        if len(self.monoholders) == 0:
            self.loci_defined = True
            return

        loci = []
        for monoholder in self.monoholders:
            monoholder.define_loci(purge=self.purge)
            for locus_instance in monoholder.loci:
                monoholder.loci[locus_instance].parent = self.id
                loci.append(monoholder.loci[locus_instance])

        for locus in sorted(loci):
            self.loci[locus.id] = locus

        self.loci_defined = True
        if self.json_conf["pick"]["alternative_splicing"]["report"] is True:
            self.define_alternative_splicing()

        return

    def define_alternative_splicing(self):

        """
         This method will consider all possible candidates for alternative splicing
         for each of the final loci, after excluding transcripts which potentially map
         to more than one Locus (in order to remove chimeras).
         It will then call the add_transcript_to_locus method to try to add
         the transcript to the relevant Locus container.
        """

        # First off, define genes

        self.define_loci()

        candidates = collections.defaultdict(set)
        primary_transcripts = set(locus.primary_transcript_id for locus in self.loci.values())

        cds_only = self.json_conf["pick"]["run_options"]["subloci_from_cds_only"]
        t_graph = self.define_graph(self.transcripts,
                                    inters=MonosublocusHolder.is_intersecting,
                                    cds_only=cds_only)
        
        cliques = self.find_cliques(t_graph)

        loci_cliques = dict()

        for lid, locus_instance in self.loci.items():
            self.loci[lid].logger = self.logger
            self.loci[lid].set_json_conf(self.json_conf)
            loci_cliques[lid] = set()
            for clique in cliques:
                if locus_instance.primary_transcript_id in clique:
                    loci_cliques[
                        locus_instance.id].update({tid for tid in clique if
                                                   tid != locus_instance.primary_transcript_id})

        for tid in iter(tid for tid in self.transcripts if tid not in primary_transcripts):
            loci_in = list(llid for llid in loci_cliques if
                           tid in loci_cliques[llid])
            if len(loci_in) == 1:
                candidates[loci_in[0]].add(tid)

        for lid in candidates:
            for tid in sorted(candidates[lid],
                              key=lambda ttid: self.transcripts[ttid].score,
                              reverse=True):
                self.loci[lid].add_transcript_to_locus(self.transcripts[tid])
            self.loci[lid].finalize_alternative_splicing()

        return

    def calculate_mono_metrics(self):
        """Wrapper to calculate the metrics for the monosubloci."""
        self.monoholders = []

        for monosublocus_instance in sorted(self.monosubloci):
            found_holder = False
            for holder in self.monoholders:
                if MonosublocusHolder.in_locus(holder, monosublocus_instance):
                    holder.add_monosublocus(monosublocus_instance)
                    found_holder = True
                    break
            if found_holder is False:
                holder = MonosublocusHolder(
                    monosublocus_instance,
                    json_conf=self.json_conf,
                    logger=self.logger)
                self.monoholders.append(holder)

        for monoholder in self.monoholders:
            if self.regressor is not None:
                monoholder.regressor = self.regressor
            monoholder.calculate_scores()

    def compile_requirements(self):
        """Quick function to evaluate the filtering expression, if it is present."""

        if "requirements" in self.json_conf:
            if "compiled" in self.json_conf["requirements"]:
                return
            else:
                self.json_conf["requirements"]["compiled"] = compile(
                    self.json_conf["requirements"]["expression"],
                    "<json>", "eval")
                return
        else:
            return

    # ############ Class methods ###########

    # The discrepancy is by design
    # pylint: disable=arguments-differ
    @classmethod
    def is_intersecting(cls, transcript, other, cds_only=False):
        """
        :rtype : bool
        :param transcript: a transcript for which we wish to verify
        whether it is intersecting with another transcript or not.
        :type transcript: Mikado.loci_objects.transcript.Transcript
        :param other: the transcript which will be used for the comparison.
        :type other: Mikado.loci_objects.transcript.Transcript

        :param cds_only: boolean flag. If enabled, only CDS exons/intron
        will be considered when deciding whether two transcripts are part
        of the same Locus or not.
        :type cds_only: bool


        When comparing two transcripts, for the definition of subloci inside
        superloci we follow these rules:

        If both are multiexonic, the function verifies whether there is at
        least one intron in common.
        If both are monoexonic, the function verifies whether there is some overlap between them.
        If one is monoexonic and the other is not, the function will return False by definition.
        """

        transcript.finalize()
        other.finalize()
        if transcript.id == other.id:
            return False  # We do not want intersection with oneself

        if transcript.monoexonic is False and other.monoexonic is False:
            if cds_only is False:
                intersection = set.intersection(transcript.introns, other.introns)
            else:
                intersection = set.intersection(transcript.combined_cds_introns,
                                                other.combined_cds_introns)
            if len(intersection) > 0:
                intersecting = True
            else:
                intersecting = False

        elif transcript.monoexonic is True and other.monoexonic is True:
            if transcript.start == other.start or transcript.end == other.end:
                intersecting = True
            else:
                test_result = cls.overlap(
                    (transcript.start, transcript.end),
                    (other.start, other.end)
                )
                intersecting = test_result > 0
        else:
            intersecting = False

        return intersecting
    # pylint: enable=arguments-differ

    # ############## Properties ############
    @property
    def id(self) -> str:
        """
        This is a generic string generator for all inherited children.
        :rtype : str
        """
        if self.stranded is True:
            strand = self.strand
        else:
            strand = "mixed"
        return "{0}:{1}{2}:{3}-{4}".format(
            self.__name__,
            self.chrom,
            strand,
            self.start,
            self.end)
