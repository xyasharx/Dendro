"""Build provenance constants stamped into distributable Dendro artifacts.

Git archives replace the ``$Format`` placeholders in this tracked template.
Wheel and source-distribution builds replace this file in their staging area,
leaving a normal development checkout unmodified.
"""

COMMIT = "$Format:%H$"
TREE = "$Format:%T$"
DIRTY = False
SOURCE = "template"
