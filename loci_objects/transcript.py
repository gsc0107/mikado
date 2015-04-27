import operator
import re
from loci_objects.abstractlocus import abstractlocus # Needed for the BronKerbosch algorithm ...

class transcript:
    
    ######### Class special methods ####################
    
    def __init__(self, gffLine):
        
        '''Initialise the transcript object, using a mRNA/transcript line.
        Note: I am assuming that the input line is an object from my own "GFF" class.
        The transcript instance must be initialised by a "(m|r|lnc|whatever)RNA" or "transcript" gffLine.'''
        
        self.chrom = gffLine.chrom
        assert "transcript"==gffLine.feature or "RNA" in gffLine.feature.upper()
        self.feature="transcript"
        self.id = gffLine.attributes["ID"]
        self.start=gffLine.start
        self.strand = gffLine.strand
        self.end=gffLine.end
        self.exons, self.cds, self.utr = [], [], []
        self.junctions = []
        self.splices = []
        self.monoexonic = False
        self.finalized = False # Flag. We do not want to repeat the finalising more than once.
        self.parent = gffLine.parent
        self.attributes = gffLine.attributes
        self.max_internal_cds_index = -1
        
    def __str__(self):
        '''Each transcript will be printed out in the GFF style.
        This is pretty rudimentary, as the class does not hold any information on the original source, feature, score, etc.'''
        
        self.finalize() #Necessary to sort the exons
        lines = []
        transcript_counter = 0
        assert self.max_internal_cds_index > -1
        
        for index in range(len(self.internal_cds)):
            
            if index==self.max_internal_cds_index: maximal=True
            else: maximal=False
            cds_run = self.internal_cds[index]
            
            if len(list(filter(lambda x: x[0]=="UTR", cds_run)  )  )>0:
                assert len(list(filter(lambda x: x[0]=="CDS", cds_run)  )  )>0
            if self.internal_cds_num>1:
                transcript_counter+=1
                tid = "{0}.orf{1}".format(self.id, transcript_counter)
            else:
                tid = self.id
            
            attr_field = "ID={0}".format(tid)
            if self.parent is not None:
                attr_field = "{0};Parent={1}".format(attr_field, self.parent)
            if self.strand is None:
                strand="."
            else:   
                strand=self.strand
            
            for attribute in self.attributes:
                if attribute in ("Parent","ID"): continue
                value=self.attributes[attribute]
                #ttribute=attribute.lower()
                attribute=re.sub(";",":", attribute.lower())
                attr_field="{0};{1}={2}".format(attr_field,attribute, value)
            
            if self.internal_cds_num>1:
                attr_field="{0};maximal={1}".format(attr_field,maximal)
            
            parent_line = [self.chrom, "locus_pipeline", "transcript", self.start, self.end, ".", strand, ".",  attr_field ]
        
            parent_line ="\t".join( str(s) for s in parent_line )
        
            exon_lines = []
        
            cds_begin = False
        
            cds_count=0
            exon_count=0
            five_utr_count=0
            three_utr_count=0

            for segment in cds_run:
                if cds_begin is False and segment[0]=="CDS": cds_begin = True
                if segment[0]=="UTR":
                    if cds_begin is True:
                        if self.strand=="-": feature="five_prime_utr"
                        else: feature="three_prime_utr"
                    else:
                        if self.strand=="-": feature="three_prime_utr"
                        else: feature="five_prime_utr"
                    if "five" in feature:
                        five_utr_count+=1
                        index=five_utr_count
                    else:
                        three_utr_count+=1
                        index=three_utr_count
                else:
                    if segment[0]=="CDS":
                        cds_count+=1
                        index=cds_count
                    else:
                        exon_count+=1
                        index=exon_count
                    feature=segment[0]
                exon_line = [self.chrom, "locus_pipeline", feature, segment[1], segment[2],
                         ".", strand, ".",
                         "ID={0}.{1}{2};Parent={0};".format(tid, feature,index) ]
                exon_lines.append("\t".join(str(s) for s in exon_line))
        
        
            lines.append(parent_line)
            lines.extend(exon_lines) 
        
        return "\n".join(lines)
    
    def __eq__(self, other):
        if not type(self)==type(other): return False
        self.finalize()
        other.finalize()
           
        if self.strand == other.strand and self.chrom == other.chrom and \
            self.start==other.start and self.end == other.end and \
            self.exons == other.exons and self.id == other.id:
            return True
          
        return False
    
    def __hash__(self):
        '''Returns the hash of the object (call to super().__hash__())'''

#         This has to be defined, otherwise the transcript objects won't be hashable
#         (and therefore operations like adding to sets will be forbidden)

        return super().__hash__()
    
    def __len__(self):
        return self.end-self.start+1

     
    def __lt__(self, other):
        if self.chrom!=other.chrom:
            return self.chrom<other.chrom
        if self==other:
            return False
        if self.start<other.start:
            return True
        elif self.start==other.start and self.end<other.end:
            return True
        return False
     
    def __gt__(self, other):
        return not self<other
     
    def __le__(self, other):
        return (self==other) or (self<other)
     
    def __ge__(self, other):
        return (self==other) or (self>other)          
    
    ######### Class instance methods ####################


    def addExon(self, gffLine):
        '''This function will append an exon/CDS feature to the object.'''

        if self.finalized is True:
            raise RuntimeError("You cannot add exons to a finalized transcript!")
        
        if gffLine.parent!=self.id:
            raise AssertionError("""Mismatch between transcript and exon:\n
            {0}\n
            {1}
            """.format(self.id, gffLine))
        if gffLine.feature=="CDS":
            store=self.cds
        elif "utr" in gffLine.feature or "UTR" in gffLine.feature:
            store=self.utr
        elif gffLine.feature=="exon":
            store=self.exons
        else:
            raise AttributeError("Unknown feature: {0}".format(gffLine.feature))
            
        start,end=sorted([gffLine.start, gffLine.end])
        store.append((start, end) )

    def finalize(self):
        '''Function to calculate the internal introns from the exons.
        In the first step, it will sort the exons by their internal coordinates.'''
        
        # We do not want to repeat this step multiple times
        if self.finalized is True:
            return

        if len(self.exons)>1 and self.strand is None:
            raise AttributeError("Multiexonic transcripts must have a defined strand! Error for {0}".format(self.id))

        self.metrics=dict() # create the store for the metrics
        if self.utr!=[] and self.cds==[]:
            raise ValueError("Transcript {tid} has defined UTRs but no CDS feature!".format(tid=self.id))

        assert self.cds_length==self.utr_length==0 or  self.cdna_length == self.utr_length + self.cds_length, (self.id, self.cdna_length, self.utr_length, self.cds_length,
                                                                                                               self.utr, self.cds, self.exons )

        self.exons = sorted(self.exons, key=operator.itemgetter(0,1) ) # Sort the exons by start then stop
        
        if self.exons[0][0]!=self.start or self.exons[-1][1]!=self.end:
            raise ValueError("The transcript {id} has coordinates {tstart}:{tend}, but its first and last exons define it up until {estart}:{eend}!".format(
                                                                                                                                                            tstart=self.start,
                                                                                                                                                            tend=self.end,
                                                                                                                                                            id=self.id,
                                                                                                                                                            eend=self.exons[-1][1],
                                                                                                                                                            estart=self.exons[0][0],
                                                                                                                                                            ))
        self.cds = sorted(self.cds, key=operator.itemgetter(0,1))
        self.utr = sorted(self.utr, key=operator.itemgetter(0,1))
        self.internal_cds, self.junctions, self.splices = [], [], []
        self.segments = [ ("exon",e[0],e[1]) for e in self.exons] + \
                    [("CDS", c[0],c[1]) for c in self.cds ] + \
                    [ ("UTR", u[0], u[1]) for u in self.utr ]
        self.segments =  sorted(self.segments, key=operator.itemgetter(1,2,0) )
        self.internal_cds.append(self.segments)
        
        if len(self.exons)==1:
            #self.finalized = True
            self.monoexonic = True
            #return # There is no sense in performing any operation on single exon transcripts
        else:
            for index in range(len(self.exons)-1):
                exonA, exonB = self.exons[index:index+2]
                if exonA[1]>=exonB[0]:
                    raise ValueError("Overlapping exons found!")
                self.junctions.append( (exonA[1]+1, exonB[0]-1) ) #Append the splice junction
                self.splices.extend( [exonA[1]+1, exonB[0]-1] ) # Append the splice locations
            
        self.junctions = set(self.junctions)
        self.splices = set(self.splices)
        _ = self.max_internal_cds
        assert self.max_internal_cds_index > -1
        
        self.finalized = True
        return


    def load_cds(self, cds_dict):
        
        '''This function is used to load the various CDSs from an external dictionary, loaded from a BED file.
        It replicates what is done internally by the "cdna_alignment_orf_to_genome_orf.pl" utility in the
        TransDecoder suite.
        Briefly, it follows this logic:
        - Finalise the transcript
        - Retrieve from the dictionary (input) the CDS object
        - Sort CDSs on the basis of their length (useful for monoexonic transcripts where we might want to set the strand)
        - For each CDS:
            - If the ORF is on the + strand:
                - all good
            - If the ORF is on the - strand:
                - if the transcript is monoexonic: invert its strand
                - if the transcript is multiexonic: skip
            - Start looking at the exons
        
        '''
        self.finalize()

        if self.id not in cds_dict:
            return        

        self.utr = []
        self.cds = []
        self.internal_cds = []
        
        self.finalized = False
        
        #Ordering the CDSs by CDS length.

        for cds_run in sorted(cds_dict[self.id], reverse=True, key=operator.attrgetter("cds_len") ):
            
            cds_start, cds_end, strand = cds_run.cdsStart, cds_run.cdsEnd, cds_run.strand
            assert cds_start>=1 and cds_end<=self.cdna_length, ( self.id, self.cdna_length, (cds_start,cds_end) )
            if self.strand is None:
                    self.strand=strand

            if strand == "-":
                if self.monoexonic is False:
                    continue
            elif self.strand is None and strand == "+":
                self.strand="+"
                
            cds_exons = []
            current_start, current_end = 0,0
            if self.strand == "+":
                for exon in sorted(self.exons, key=operator.itemgetter(0,1)):
                    cds_exons.append(("exon", exon[0], exon[1] ) )
                    current_start+=1
                    current_end+=exon[1]-exon[0]+1
                    #Whole UTR
                    if current_end<cds_start or current_start>cds_end:
                        cds_exons.append( ("UTR", exon[0], exon[1])  )
                    else:
                        c_start = exon[0] + max(0, cds_start-current_start )
                        if c_start > exon[0]:
                            u_end = c_start-1
                            cds_exons.append( ("UTR", exon[0], u_end) )
                        c_end = exon[1] - max(0, current_end - cds_end ) 
                        if c_start<c_end:
                            cds_exons.append(("CDS", c_start, c_end))
                        if c_end < exon[1]:
                            cds_exons.append( ("UTR", c_end+1, exon[1]  ) )
                    current_start=current_end
                            
            elif self.strand=="-":
                for exon in sorted(self.exons, key=operator.itemgetter(0,1), reverse=True):
                    cds_exons.append(("exon", exon[0], exon[1] ) )
                    current_start+=1
                    current_end+=exon[1]-exon[0]+1
                    if current_end<cds_start or current_start>cds_end:
                        cds_exons.append( ("UTR", exon[0], exon[1] ))
                    else:
                        c_end = exon[1] - max(0,cds_start - current_start ) 
                        assert c_end>exon[0]
                        if c_end < exon[1]:
                            cds_exons.append(("UTR", c_end+1, exon[1]))
                        c_start = exon[0] + max(0, current_end - cds_end )
                        cds_exons.append( ("CDS", c_start, c_end) )
                        if c_start>exon[0]:
                            cds_exons.append( ("UTR", exon[0], c_start-1) )
                    current_start=current_end
        
            self.internal_cds.append( sorted(cds_exons, key=operator.itemgetter(1,2)   ) )
            
        if len(self.internal_cds)==1:
            self.cds = sorted(
                              [(a[1],a[2]) for a in filter(lambda x: x[0]=="CDS", self.internal_cds[0])],
                              key=operator.itemgetter(0,1)
                              
                              )
            self.utr = sorted(
                              [(a[1],a[2]) for a in filter(lambda x: x[0]=="UTR", self.internal_cds[0])],
                              key=operator.itemgetter(0,1)
                              
                              )
            
            
        elif len(self.internal_cds)>1:
            
            cds_spans = []
            candidates = []
            for internal_cds in self.internal_cds:
                candidates.extend([tuple([a[1],a[2]]) for a in filter(lambda tup: tup[0]=="CDS", internal_cds  )])
                              
            candidates=set(candidates)
            original=candidates.copy()
            for mc in self.merge_cliques(list(self.BronKerbosch(set(), candidates, set(), original,
                                                           ))):
                span=tuple([min(t[0] for t in mc),
                            max(t[1] for t in mc)                        
                            ])
                cds_spans.append(span)
                
            cds_spans=sorted(cds_spans, key = operator.itemgetter(0,1))
            self.cds = cds_spans
            
            #This method is probably OBSCENELY inefficient, but I cannot think of a better one for the moment.
            curr_utr_segment = None

            utr_pos = set.difference( 
                                                  set.union(*[ set(range(exon[0],exon[1]+1)) for exon in self.exons]),
                                                  set.union(*[ set(range(cds[0],cds[1]+1)) for cds in self.cds])
                                                  )
            for pos in sorted(list(utr_pos)):
                if curr_utr_segment is None:
                    curr_utr_segment = (pos,pos)
                else:
                    if pos==curr_utr_segment[1]+1:
                        curr_utr_segment = (curr_utr_segment[0],pos)
                    else:
                        self.utr.append(curr_utr_segment)
                        curr_utr_segment = (pos,pos)
                        
            if curr_utr_segment is not None:
                self.utr.append(curr_utr_segment)           
                                   
            assert self.cdna_length == self.cds_length + self.utr_length, (self.cdna_length, self.cds, self.utr)                            
        
        if self.internal_cds == []:
            self.finalize()
        else:
            self.finalized=True
        return
                        

      
    @classmethod
    ####################Class methods#####################################  
    def BronKerbosch(cls, clique, candidates, non_clique, original):
        '''Wrapper for the abstractlocus method. It will pass to the function the class's "is_intersecting" method
        (which would be otherwise be inaccessible from the abstractlocus class method)'''
        for clique in abstractlocus.BronKerbosch(clique, candidates, non_clique, original,
                                                 inters=cls.is_intersecting,
                                                 neighbours = None   ):
            yield clique
    
    @classmethod
    def is_intersecting(cls, first, second):
        '''Implementation of the is_intersecting method.'''
        if first==second or cls.overlap(first,second)<=0:
            return False
        return True

    @classmethod
    def overlap(cls, first,second):
        lend = max(first[0], second[0])
        rend = min(first[1], second[1])
        return rend-lend
    
    @classmethod
    def merge_cliques(cls, cliques):
        '''Wrapper for the abstractlocus method.'''
        return abstractlocus.merge_cliques(cliques)

    ####################Class properties##################################

    @property
    def cds_length(self):
        '''This property return the length of the CDS part of the transcript.'''
        return sum([ c[1]-c[0]+1 for c in self.cds ])
            
    @property
    def utr_length(self):
        '''This property return the length of the UTR part of the transcript.'''
        return sum([ e[1]-e[0]+1 for e in self.utr ])
        
    @property
    def cdna_length(self):
        '''This property returns the length of the transcript.'''
        return sum([ e[1]-e[0]+1 for e in self.exons ])
    
    @property
    def internal_cds_num(self):
        '''This property returns the number of CDSs inside a transcript.'''
        
        return len(self.internal_cds)

#     @property
#     def internal_cds(self):
#         '''This property calculates the CDSs inside a transcript.
#         The property is a list of tuples, each of which
#         describes a CDS segment and holds as tuples (start, end) the 
#         CDS segments inside the single CDS.'''
#         
#         self.finalize()
#         self.__internal_cds = []
#         if len(self.utr)==0:
#             self.__internal_cds = [ tuple(self.cds) ] 
#         elif len(self.cds)>0:
#             current_cds=[]
#             in_utr=True
#             #The sense of this cycle is to look for multiple CDSs. I exploit the fact that 
#             #the transcript is a directed segment .. so by parsing linearly I can detect easily
#             #instances where I have a UTR placed between two CDSs
#             for segment in filter(lambda x: x[0]!="exon", self.segments):
#                 if segment[0]=="CDS":
#                     if in_utr is True:
#                         in_utr=False
#                     current_cds.append(tuple([segment[1],segment[2]]))
#                 elif segment[0]=="UTR":
#                     if in_utr is False:
#                         if len(current_cds)>0:
#                             self.__internal_cds.append(tuple(current_cds) )
#                         current_cds=[]
#                     in_utr=True
#             if len(current_cds)>0:  
#                 self.__internal_cds.append(tuple(current_cds))
#         assert sum(len(x) for x in self.__internal_cds) == len(self.cds)
#         return self.__internal_cds
    
    @property
    def max_internal_cds_length(self):
        '''This property calculates the length of the greatest CDS inside the cDNA.'''
        if len(self.cds)==0:
            self.__max_internal_cds_length=0
        else:
            self.__max_internal_cds_length=sum(x[2]-x[1]+1 for x in filter(lambda x: x[0]=="CDS", self.max_internal_cds))
        return self.__max_internal_cds_length

    @property
    def max_internal_cds(self):
        '''This property will return the tuple of tuples of the CDS with the greatest length
        inside the transcript. To avoid memory wasting, the tuple is accessed in real-time using 
        a token (__max_internal_cds_index) which holds the position in the __internal_cds list of the longest CDS.'''
        if len(self.cds)==0: # Non-sense to calculate the maximum CDS for transcripts without it
            self.__max_internal_cds_length=0
            self.max_internal_cds_index=0
            return tuple([])
        
        elif self.max_internal_cds_index==-1:
            greatest=0
            self.max_internal_cds_index=-1
            for index in range(len(self.internal_cds)):
                cds=self.internal_cds[index]
                length = sum( c[2]-c[1]+1  for c in filter(lambda x: x[0]=="CDS",  cds  ))
                    
                if length>greatest:
                    #self.__max_internal_cds_length=length
                    self.max_internal_cds_index=index
            if self.max_internal_cds_index==-1: raise ValueError("""Index not modified for transcript {0}!
            Monoexonic: {1}; CDS length: {2};
            CDS: {3};
            greatest: {4};
            internal: {5};
            __internal: {6}""".format(self.id,
                               self.monoexonic,
                               self.cds_length,
                               self.cds,
                               greatest,
                               self.internal_cds,
                               self.__internal_cds))
        return self.internal_cds[self.max_internal_cds_index]

    @max_internal_cds.setter
    def max_internal_cds(self,*args):
        if len(args)==0:
            args.append(None)
        assert len(args)==1
        self.__max_internal_cds=args[0]
    

    @property
    def max_internal_cds_index(self):
        return self.__max_internal_cds_index
    
    @max_internal_cds_index.setter
    def max_internal_cds_index(self, index):
        if type(index) is not int:
            raise TypeError()
        self.__max_internal_cds_index = index
        
    @property
    def internal_cds_lengths(self):
        lengths = []
        for internal_cds in self.internal_cds:
            lengths.append( sum( x[2]-x[1]+1 for x in filter(lambda c: c[0]=="CDS", internal_cds) ) )
        lengths = sorted(lengths, reverse=True)
        return lengths
        