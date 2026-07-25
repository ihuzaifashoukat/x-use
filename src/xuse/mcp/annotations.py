"""Machine-readable behaviour hints for every registered tool.

x-use has always had a read-only vs write boundary, but until now it existed
only in prose inside tool descriptions. A client could not enforce it, and
directory scanners that read MCP annotations as the authoritative capability
declaration saw nothing at all.

These constants encode the boundary the codebase already follows, so a host can
auto-approve reads, prompt on local writes, and hard-gate anything that reaches
x.com. Names describe the blast radius, not the implementation, because that is
what a calling agent has to reason about.

The four hints come from the MCP spec and mean exactly what it says:

- ``readOnlyHint``    the tool does not modify its environment.
- ``destructiveHint`` the tool may remove or overwrite existing state, as
                      opposed to only adding to it. Meaningful only when the
                      tool is not read-only.
- ``idempotentHint``  calling again with the same arguments changes nothing
                      further. Meaningful only when the tool is not read-only.
- ``openWorldHint``   the tool talks to an external system, so its result is
                      not a pure function of local state.

A note on draft mode, which is the easy thing to get wrong here. Write tools
stage a draft and touch nothing on X, but a draft is still a local record, so
they are not read-only. Their hints describe the tool's own effect. The two
gates that actually publish, ``approve_draft`` and ``process_queue``, are the
ones that carry ``openWorldHint=True`` for a state change on X.

Publishing is annotated ``destructiveHint=False`` deliberately: posting adds a
tweet rather than overwriting anything, which is what the spec's additive vs
destructive distinction asks about. It is irreversible and public, but that is
what the approval gates are for, and misreporting it as destructive would make
the flag useless for the tools that genuinely delete config.
"""

from mcp.types import ToolAnnotations

# Reads that never leave the machine: config, drafts, queue, metrics, run state.
READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=False,
)

# Reads that scrape x.com through the browser. Still no state change, but the
# answer depends on the network and on whoever posted since the last call.
READ_ONLY_FROM_X = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=True,
)

# Adds to local state (a draft, a queue entry, a new account or proxy pool).
# Nothing on X moves. Calling twice creates a second record.
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

# Local state change that settles into one end state: flipping a flag, patching
# fields, marking something rejected or cancelled. Repeat calls are no-ops.
LOCAL_WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# Removes local configuration. Idempotent because the end state is "gone", and
# these are the tools that genuinely warrant a confirmation prompt.
LOCAL_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

# Reaches x.com and changes something there: posts, replies, likes, retweets.
# Every tool carrying this either is an approval gate or bypasses draft mode.
PUBLISHES_TO_X = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

# Calls the configured LLM and stages the result locally. Open-world because the
# text comes from a model over the network, but nothing is published.
STAGES_GENERATED_DRAFT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

__all__ = [
    "READ_ONLY_LOCAL",
    "READ_ONLY_FROM_X",
    "LOCAL_WRITE",
    "LOCAL_WRITE_IDEMPOTENT",
    "LOCAL_DESTRUCTIVE",
    "PUBLISHES_TO_X",
    "STAGES_GENERATED_DRAFT",
]
