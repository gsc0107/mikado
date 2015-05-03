from loci_objects.sublocus import sublocus
from loci_objects.transcript import transcript
import re

'''Quick script to automate the generation of metrics definition from the files.'''


metrics = sublocus.available_metrics

not_found=[]

print()

for metric in ["tid", "parent", "score"]+sorted(filter(lambda x: x not in ("tid", "parent", "score", metrics), metrics)):

    if hasattr(transcript, metric):
        print( "- *{0}*:".format(metric), re.sub(" +", " ", re.sub("\n", " ", getattr(transcript,metric).__doc__ )), sep="\t")
    else:
        not_found.append(metric)
       
print() 
if len(not_found)>0:
    print("\nMetrics not found:\n\t{0}\n\t".format("\n\t".join(not_found)))
    print()