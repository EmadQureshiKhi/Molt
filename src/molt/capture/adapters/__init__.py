"""One adapter module per supported agent command-line tool hook format.

Each of the five modules is written from that tool's own published hook
specification and binds the module attribute the entry point loads by name. No
adapter imports another, and the only code they share is `builders`, which
constructs Events and renders a recall block, and `invocation_index`, which holds a
tool call's Event identifier between the two hook processes that observe the call
and its result. Neither shared module reads a vendor payload field, because sharing
that reading would force one tool's payload shape onto another (Requirement 1.9).
"""
