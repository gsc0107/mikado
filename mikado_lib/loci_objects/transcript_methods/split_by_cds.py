from collections import OrderedDict
import collections
import operator
from intervaltree import IntervalTree, Interval
from mikado_lib.loci_objects.abstractlocus import Abstractlocus
from mikado_lib.exceptions import InvalidTranscript

__author__ = 'luca'


def check_split_by_blast(self, cds_boundaries):

    """
    This method verifies if a transcript with multiple ORFs has support by BLAST to
    NOT split it into its different components.

    The minimal overlap between ORF and HSP is defined inside the JSON at the key
        ["chimera_split"]["blast_params"]["minimal_hsp_overlap"]
    basically, we consider a HSP a hit only if the overlap is over a certain threshold
    and the HSP evalue under a certain threshold.

    The split by CDS can be executed in three different ways - CLEMENT, LENIENT, STRINGENT:

    - PERMISSIVE: split if two CDSs do not have hits in common,
    even when one or both do not have a hit at all.
    - STRINGENT: split only if two CDSs have hits and none
    of those is in common between them.
    - LENIENT: split if *both* lack hits, OR *both* have hits and none
    of those is in common.


    :param cds_boundaries:
    :return:
    """

    # Establish the minimum overlap between an ORF and a BLAST hit to consider it
    # to establish belongingness

    minimal_overlap = self.json_conf["chimera_split"]["blast_params"]["minimal_hsp_overlap"]

    cds_hit_dict = OrderedDict().fromkeys(cds_boundaries.keys())
    for key in cds_hit_dict:
        cds_hit_dict[key] = collections.defaultdict(list)

    # BUG, this is a hacky fix
    if not hasattr(self, "blast_hits"):
        self.logger.warning(
            "BLAST hits store lost for %s! Creating a mock one to avoid a crash",

            self.id)
        self.blast_hits = []

    # Determine for each CDS which are the hits available
    min_eval = self.json_conf['chimera_split']['blast_params']['hsp_evalue']
    for hit in self.blast_hits:
        for hsp in iter(_hsp for _hsp in hit["hsps"] if
                        _hsp["hsp_evalue"] <= min_eval):
            for cds_run in cds_boundaries:
                # If I have a valid hit b/w the CDS region and the hit,
                # add the name to the set
                overlap_threshold = minimal_overlap * (cds_run[1] + 1 - cds_run[0])
                if Abstractlocus.overlap(cds_run, (
                        hsp['query_hsp_start'],
                        hsp['query_hsp_end'])) >= overlap_threshold:
                    cds_hit_dict[cds_run][(hit["target"], hit["target_length"])].append(hsp)

    final_boundaries = OrderedDict()
    for boundary in self.__get_boundaries_from_blast(cds_boundaries, cds_hit_dict):
        if len(boundary) == 1:
            assert len(boundary[0]) == 2
            boundary = boundary[0]
            final_boundaries[boundary] = cds_boundaries[boundary]
        else:
            nboun = (boundary[0][0], boundary[-1][1])
            final_boundaries[nboun] = []
            for boun in boundary:
                final_boundaries[nboun].extend(cds_boundaries[boun])

    cds_boundaries = final_boundaries.copy()
    return cds_boundaries


def check_common_hits(self, cds_hits, old_hits):
    """
    This private method verifies whether we have to split a transcript
    if there are hits for both ORFs and some of them refer to the same target.
    To do so, we check whether the two CDS runs actually share at least one HSPs
    (in which case we do NOT want to split); if not, we verify whether the HSPs
    cover a large fraction of the target length. If this is the case, we decide to
    break down the transcript because we are probably in the presence of a tandem
    duplication.
    :param cds_hits:
    :param old_hits:
    :return:
    """

    in_common = set.intersection(set(cds_hits.keys()),
                                 set(old_hits.keys()))
    # We do not have any hit in common
    to_break = len(in_common) == 0
    min_overlap_duplication = self.json_conf[
        'chimera_split']['blast_params']['min_overlap_duplication']
    to_break = True
    for common_hit in in_common:
        old_hsps = old_hits[common_hit]
        cds_hsps = cds_hits[common_hit]
        # First check ... do we have HSPs in common?
        # if len(set.intersection(old_hsps, cds_hsps)) > 0:
        #     to_break = False
        #     break
        old_query_boundaries = IntervalTree([Interval(h["query_hsp_start"],
                                                      h["query_hsp_end"])
                                             for h in old_hsps])
        # Look for HSPs that span the two ORFs
        if any([len(
                old_query_boundaries.search(new_cds["query_hsp_start"],
                                            new_cds["query_hsp_end"])) > 0]
               for new_cds in cds_hsps):
            to_break = False

        old_target_boundaries = IntervalTree([
            Interval(h["target_hsp_start"], h["target_hsp_end"]) for h in old_hsps])
        for cds_hsp in cds_hsps:
            boundary = (cds_hsp["target_hsp_start"], cds_hsp["target_hsp_end"])
            for target_hit in old_target_boundaries.search(*boundary):
                overlap_fraction = self.overlap(boundary,
                                                target_hit)/common_hit[1]

                if overlap_fraction >= min_overlap_duplication:
                    to_break = True and to_break
                else:
                    to_break = False
    return to_break


def __get_boundaries_from_blast(self, cds_boundaries, cds_hit_dict):

    """
    Private method that calculates the CDS boundaries to keep
    given the blast hits. Called by check_split_by_blast
    :param cds_boundaries:
    :return:
    """
    new_boundaries = []
    leniency = self.json_conf['chimera_split']['blast_params']['leniency']
    for cds_boundary in cds_boundaries:
        if not new_boundaries:
            new_boundaries.append([cds_boundary])
        else:
            old_boundary = new_boundaries[-1][-1]
            cds_hits = cds_hit_dict[cds_boundary]
            old_hits = cds_hit_dict[old_boundary]
            if len(cds_hits) == len(old_hits) == 0:  # No hit found for either CDS
                # If we are stringent, we DO NOT split
                if leniency == "STRINGENT":
                    new_boundaries[-1].append(cds_boundary)
                else:  # Otherwise, we do split
                    new_boundaries.append([cds_boundary])
            elif min(len(cds_hits), len(old_hits)) == 0:  # We have hits for only one
                # If we are permissive, we split
                if leniency == "PERMISSIVE":
                    new_boundaries.append([cds_boundary])
                else:
                    new_boundaries[-1].append(cds_boundary)
            else:
                if self.__check_common_hits(cds_hits, old_hits) is True:
                    new_boundaries.append([cds_boundary])
                # We have hits in common
                else:
                    new_boundaries[-1].append(cds_boundary)
        # } # Finish BLAST check
    return new_boundaries


def __split_complex_exon(self, exon, texon, left, right, boundary):

    """
    Private method used to split an exon when it is only partially coding,
    :param exon: Exon to be analysed
    :param texon: Transcriptomic coordinates of the exon
    :param left: boolean flag, it indicates wheter there is another transcript
    on the left of the current one.
    :param right: boolean flag, it indicates wheter there is another transcript
    on the left of the current one.
    :param boundary: Transcriptomic coordinates of the ORF boundary.
    :return:
    """

    to_discard = None
    new_exon = list(exon)

    if texon[1] == boundary[0]:
        # In this case we have that the exon ends exactly at the end of the
        # UTR, so we have to keep a one-base exon
        if left is False:
            self.logger.debug("Appending mixed UTR/CDS 5' exon %s", exon)
        else:
            if self.strand == "+":
                # Keep only the LAST base
                to_discard = (exon[0], exon[1]-1)
                new_exon = (exon[1]-1, exon[1])
                texon = (texon[1]-1, texon[1])
                self.logger.debug("Appending monobase CDS exon %s (Texon %s)",
                                  new_exon,
                                  texon)
            else:
                # Keep only the FIRST base
                to_discard = (exon[0]+1, exon[1])
                new_exon = (exon[0], exon[0]+1)
                texon = (texon[1]-1, texon[1])
                self.logger.debug(
                    "Appending monobase CDS exon %s (Texon %s)",
                    new_exon,
                    texon)

    elif texon[0] == boundary[1]:
        # In this case we have that the exon ends exactly at the end of the
        # CDS, so we have to keep a one-base exon
        if right is False:
            self.logger.debug(
                "Appending mixed UTR/CDS right exon %s",
                exon)
        else:
            if self.strand == "+":
                # In this case we have to keep only the FIRST base
                to_discard = (exon[0]+1, exon[1])
                new_exon = (exon[0], exon[0]+1)
                texon = (texon[0], texon[0]+1)
                self.logger.debug(
                    "Appending monobase CDS exon %s (Texon %s)",
                    new_exon,
                    texon)
            else:
                # In this case we have to keep only the LAST base
                to_discard = (exon[0], exon[1]-1)
                new_exon = (exon[1]-1, exon[1])
                texon = (texon[0], texon[0]+1)
                self.logger.debug(
                    "Appending monobase CDS exon %s (Texon %s)",
                    new_exon,
                    texon)

    elif texon[0] <= boundary[0] <= boundary[1] <= texon[1]:
        # Monoexonic
        self.logger.debug("Exon %s, case 3.1", exon)
        if self.strand == "-":
            if left is True:
                new_exon[1] = exon[0] + (texon[1] - boundary[0])
            if right is True:
                new_exon[0] = exon[1] - (boundary[1] - texon[0])
        else:
            if left is True:
                new_exon[0] = exon[1] - (texon[1] - boundary[0])
            if right is True:
                new_exon[1] = exon[0] + (boundary[1] - texon[0])
        self.logger.debug(
            "[Monoexonic] Tstart shifted for %s, %d to %d",
            self.id, texon[0], boundary[0])
        self.logger.debug(
            "[Monoexonic] GStart shifted for %s, %d to %d",
            self.id, exon[0], new_exon[1])
        self.logger.debug(
            "[Monoexonic] Tend shifted for %s, %d to %d",
            self.id, texon[1], boundary[1])
        self.logger.debug(
            "[Monoexonic] Gend shifted for %s, %d to %d",
            self.id, exon[1], new_exon[1])

        if left is True:
            texon[0] = boundary[0]
        if right is True:
            texon[1] = boundary[1]

    elif texon[0] <= boundary[0] <= texon[1] <= boundary[1]:
        # In this case we have that exon is sitting halfway
        # i.e. there is a partial 5'UTR
        if left is True:
            if self.strand == "-":
                new_exon[1] = exon[0] + (texon[1] - boundary[0])
            else:
                new_exon[0] = exon[1] - (texon[1] - boundary[0])
            self.logger.debug(
                "Tstart shifted for %s, %d to %d", self.id, texon[0], boundary[0])
            self.logger.debug(
                "GStart shifted for %s, %d to %d", self.id, exon[0], new_exon[1])
            texon[0] = boundary[0]

    elif texon[1] >= boundary[1] >= texon[0] >= boundary[0]:
        # In this case we have that exon is sitting halfway
        # i.e. there is a partial 3'UTR
        if right is True:
            if self.strand == "-":
                new_exon[0] = exon[1] - (boundary[1] - texon[0])
            else:
                new_exon[1] = exon[0] + (boundary[1] - texon[0])
            self.logger.debug(
                "Tend shifted for %s, %d to %d",
                self.id, texon[1], boundary[1])
            self.logger.debug(
                "Gend shifted for %s, %d to %d",
                self.id, exon[1], new_exon[1])
            texon[1] = boundary[1]
        else:
            self.logger.debug("New exon: %s", new_exon)
            self.logger.debug("New texon: %s", texon)

    # Prevent monobase exons
    if new_exon[0] == new_exon[1]:
        new_exon[1] += 1

    return new_exon, texon, to_discard

def __create_splitted_exons(self, boundary, left, right):

    """
    Given a boundary in transcriptomic coordinates, this method will extract the
    exons retained in the splitted part of the model.

    :param boundary: the *transcriptomic* coordinates of start/end of the ORF(s)
    to be included in the new transcript
    :type boundary: (int,int)

    :param left: boolean flag indicating whether there is another sub-transcript
    to the left of the one we mean to create, irrespective of *genomic* strand
    :type left: bool

    :param left: boolean flag indicating whether there is another sub-transcript
    to the right of the one we mean to create, irrespective of *genomic* strand
    :type right: bool


    :return: my_exons (final exons), discarded_exons (eventual discarded exons),
    tstart (new transcript start), tend (new transcript end)
    :rtype: (list(int,int),list(int,int),int,int)
    """

    my_exons = []

    discarded_exons = []
    tlength = 0
    tstart = float("Inf")
    tend = float("-Inf")

    if self.strand == "-":
        reversal = True
    else:
        reversal = False

    for exon in sorted(self.exons, key=operator.itemgetter(0), reverse=reversal):
        # Translate into transcript coordinates
        elength = exon[1] - exon[0] + 1
        texon = [tlength + 1, tlength + elength]
        tlength += elength
        self.logger.debug("Analysing exon %s [%s] for %s",
                          exon, texon, self.id)

        # SIMPLE CASES
        # Exon completely contained in the ORF
        if boundary[0] <= texon[0] < texon[1] <= boundary[1]:
            self.logger.debug("Appending CDS exon %s", exon)
            my_exons.append(exon)
        # Exon on the left of the CDS
        elif texon[1] < boundary[0]:
            if left is False:
                self.logger.debug("Appending 5'UTR exon %s", exon)
                my_exons.append(exon)
            else:
                self.logger.debug("Discarding 5'UTR exon %s", exon)
                discarded_exons.append(exon)
                continue
        elif texon[0] > boundary[1]:
            if right is False:
                self.logger.debug("Appending 3'UTR exon %s", exon)
                my_exons.append(exon)
            else:
                self.logger.debug("Discarding 3'UTR exon %s", exon)
                discarded_exons.append(exon)
                continue
        else:
            # exon with partial UTR, go to the relative function
            # to handle these complex cases
            exon, texon, to_discard = self.__split_complex_exon(
                exon, texon, left, right, boundary)
            my_exons.append(tuple(sorted(exon)))
            if to_discard is not None:
                discarded_exons.append(to_discard)

        tstart = min(tstart, texon[0])
        tend = max(tend, texon[1])

    return my_exons, discarded_exons, tstart, tend

def __create_splitted_transcripts(self, cds_boundaries):

    """
    Private method called by split_by_cds to create the various (N>1) transcripts
    that are its output.
    :param cds_boundaries: a list of int tuples, containing the boundaries
     of the new transcripts.
    :return:
    """

    spans = []
    new_transcripts = []

    for counter, (boundary, bed12_objects) in enumerate(
            sorted(cds_boundaries.items(),
                   key=operator.itemgetter(0))):
        new_transcript = self.__class__()
        new_transcript.feature = "mRNA"
        for attribute in ["chrom", "source", "score", "strand", "attributes"]:
            setattr(new_transcript, attribute, getattr(self, attribute))
        # Determine which ORFs I have on my right and left
        new_transcript.parent = self.parent
        left = True
        right = True
        if counter == 0:  # leftmost
            left = False
        if 1 + counter == len(cds_boundaries):  # rightmost
            right = False
        counter += 1  # Otherwise they start from 0
        new_transcript.id = "{0}.split{1}".format(self.id, counter)
        new_transcript.logger = self.logger

        my_exons, discarded_exons, tstart, tend = self.__create_splitted_exons(
            boundary, left, right)

        self.logger.debug("""TID %s counter %d, boundary %s, left %s right %s""",
                          self.id,
                          counter,
                          boundary,
                          left,
                          right)

        if right is True:
            self.logger.debug("TID %s TEND %d Boun[1] %s",
                              self.id, tend, boundary[1])
        if left is True:
            self.logger.debug("TID %s TSTART %d Boun[0] %s",
                              self.id, tstart, boundary[0])

        assert len(my_exons) > 0, (discarded_exons, boundary)

        new_transcript.exons = my_exons

        new_transcript.start = min(exon[0] for exon in new_transcript.exons)
        new_transcript.end = max(exon[1] for exon in new_transcript.exons)
        new_transcript.json_conf = self.json_conf
        # Now we have to modify the BED12s to reflect
        # the fact that we are starting/ending earlier
        new_transcript.finalize()
        if new_transcript.monoexonic is True:
            new_transcript.strand = None

        new_bed12s = self.relocate_orfs(bed12_objects, tstart, tend)
        self.logger.debug("Loading %d ORFs into the new transcript",
                          len(new_bed12s))
        new_transcript.load_orfs(new_bed12s)

        if new_transcript.selected_cds_length <= 0:
            err_message = "No CDS information retained for {0} split {1}\n".format(
                self.id, counter)
            err_message += "BED: {0}".format("\n\t".join([str(x) for x in new_bed12s]))
            raise InvalidTranscript(err_message)

        for hit in self.blast_hits:
            if Abstractlocus.overlap((hit["query_start"], hit["query_end"]), (boundary)) > 0:
                new_hit = self.__recalculate_hit(hit, boundary)
                if new_hit is not None:
                    self.logger.debug("""Hit %s,
                    previous id/query_al_length/t_al_length %f/%f/%f,
                    novel %f/%f/%f""",
                                      new_hit["target"],
                                      hit["global_identity"],
                                      hit["query_aligned_length"],
                                      hit["target_aligned_length"],
                                      new_hit["global_identity"],
                                      new_hit["query_aligned_length"],
                                      new_hit["target_aligned_length"])

                    new_transcript.blast_hits.append(new_hit)
                else:
                    self.logger.debug("Hit %s did not pass overlap checks for %s",
                                      hit["target"], new_transcript.id)
            else:
                self.logger.debug("Ignoring hit {0} as it is not intersecting")
                continue

        new_transcripts.append(new_transcript)
        nspan = (new_transcript.start, new_transcript.end)
        self.logger.debug(
            "Transcript {0} split {1}, discarded exons: {2}".format(
                self.id, counter, discarded_exons))
        self.__check_collisions(nspan, spans)
        spans.append([new_transcript.start, new_transcript.end])

    return new_transcripts

def __recalculate_hit(self, hit, boundary):
    """Static method to recalculate coverage/identity for new hits."""

    __valid_matches = set([chr(x) for x in range(65, 91)] + [chr(x) for x in range(97, 123)] +
                          ["|"])

    hit_dict = dict()
    for key in iter(k for k in hit.keys() if k not in ("hsps",)):
        hit_dict[key] = hit[key]

    hsp_dict_list = []
    # hit_dict["global_identity"] = []
    q_intervals = []
    t_intervals = []

    identical_positions, positives = set(), set()

    minimal_overlap = self.json_conf["chimera_split"]["blast_params"]["minimal_hsp_overlap"]

    best_hsp = (float("inf"), float("-inf"))

    for hsp in hit["hsps"]:
        _ = Abstractlocus.overlap((hsp["query_hsp_start"], hsp["query_hsp_end"]), boundary)
        if _ >= minimal_overlap * (boundary[1] + 1 - boundary[0]):
            hsp_dict_list.append(hsp)
            if hsp["hsp_evalue"] < best_hsp[0]:
                best_hsp = (hsp["hsp_evalue"], hsp["hsp_bits"])

            q_intervals.append((hsp["query_hsp_start"], hsp["query_hsp_end"]))
            t_intervals.append((hsp["target_hsp_start"], hsp["target_hsp_end"]))

            query_pos = hsp["query_hsp_start"] - 1

            for amino in hsp["match"]:
                if amino in __valid_matches or amino == "+":
                    query_pos += 1
                    positives.add(query_pos)
                    if amino != "+":
                        identical_positions.add(query_pos)
                elif amino == "_":  # Gap in the target sequence
                    query_pos += 1

    if len(hsp_dict_list) == 0:
        return None

    q_merged_intervals = sorted(merge(q_intervals), key=operator.itemgetter(0, 1))
    q_aligned = sum([tup[1] - tup[0] + 1 for tup in q_merged_intervals])
    hit_dict["query_aligned_length"] = q_aligned
    hit_dict["query_start"] = q_merged_intervals[0][0]
    hit_dict["query_end"] = q_merged_intervals[-1][1]

    t_merged_intervals = sorted(merge(t_intervals), key=operator.itemgetter(0, 1))
    t_aligned = sum([tup[1] - tup[0] + 1 for tup in t_merged_intervals])
    hit_dict["target_aligned_length"] = t_aligned
    hit_dict["target_start"] = t_merged_intervals[0][0]
    hit_dict["target_end"] = t_merged_intervals[-1][1]
    hit_dict["global_identity"] = len(identical_positions) * 100 / q_aligned
    hit_dict["global_positives"] = len(positives) * 100 / q_aligned
    hit_dict["hsps"] = hsp_dict_list
    hit_dict["bits"] = max(x["hsp_bits"] for x in hit_dict["hsps"])
    hit_dict["evalue"] = min(x["hsp_evalue"] for x in hit_dict["hsps"])

    return hit_dict


def split_by_cds(self):
        self.finalize()

        # List of the transcript that will be retained

        if self.number_internal_orfs < 2:
            new_transcripts = [self]  # If we only have one ORF this is easy
        else:

            cds_boundaries = OrderedDict()
            for orf in sorted(self.loaded_bed12,
                              key=operator.attrgetter("thick_start", "thick_end")):
                cds_boundaries[(orf.thick_start, orf.thick_end)] = [orf]

            # Check whether we have to split or not based on BLAST data
            if self.json_conf is not None:
                if self.json_conf["chimera_split"]["blast_check"] is True:
                    cds_boundaries = self.check_split_by_blast(cds_boundaries)

            if len(cds_boundaries) == 1:
                # Recheck how many boundaries we have - after the BLAST check
                # we might have determined that the transcript has not to be split
                new_transcripts = [self]
            else:
                new_transcripts = __create_splitted_transcripts(cds_boundaries)

        assert len(new_transcripts) > 0, str(self)
        for new_transc in new_transcripts:
            yield new_transc

        return
