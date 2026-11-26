# Permit pairs: the platform agent against a profile on the same ask

**Status:** proposal, for the first of the two comparisons of
[#6539](https://github.com/gke-labs/kube-agents/issues/6539). The pair mode of the compare, the
pairs file and the first pair exist; the form and the "Done when" list are open for the
maintainers' agreement.

## The problem

The move of the platform agent's work to AgentProfiles
([#6526](https://github.com/gke-labs/kube-agents/issues/6526)) narrows what a worker may do: a
profile task writes under a permit minted for that task, where the platform agent writes with the
install's authority. The move is safe to finish when each capability still works under the narrow
permit. The second comparison of #6539, the two front doors, runs the same cases on two installs
(`scripts/eval_front_door_ab.py`). This comparison differs in who gets the ask, so its two sides
are two cases, and they can run on one install.

## The form: a pair of cases

A pair is two cases with the same ask and the same checks. Side A has no `[bench:profile ...]`
directive: the ask goes to the install's default route, and the platform agent does the work
with today's authority. Side B opens with the directive, so the ask goes to a profile as a new
top task, with the permits that the dispatcher mints for it.

`hack/eval/permit-pairs.txt` lists the pairs, one line each. `make bench-case-check` holds each
line to this rule, so the two sides cannot drift apart:

| What             | Rule                                                                                                                                                                                                                                        |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The ask          | Side A's prompt is one target sentence, then side B's ask word for word. The sentence names the directive's parameters in their order: `The target of this task: <key> <value>, ...` A directive with no parameters has no target sentence. |
| The checks       | `verification_spec` is equal entry by entry. Only an entry's `name` may differ.                                                                                                                                                             |
| Each other field | Equal, but the ones that name the case: `id`, `name`, `expected_output`, `owner`, `domain`.                                                                                                                                                 |
| The lane         | Two sides that are writers are in the same third of the nightly's writers part, so they run on one install at one commit. Two sides that are not writers can run in different shards of the main part.                                      |

A pull request that changes one side changes the other in the same pull request, or removes the
pair's line. The lint's finding says so. The first pair uses an existing side B
(`cluster-profile-confirmed-pdb-pull-request`), so this rule now also applies to that case.

The route differs between the sides by design, and so does the structured confirm of a side B
that writes (`intent=confirm`; side A is a typed message). That difference is what the pair
measures.

## The compare

`scripts/eval_front_door_ab.py --pairs hack/eval/permit-pairs.txt <artifact directory> [...]`
reads the `case-<name>.json` hand-offs of one or more runs. Each line of the pairs file with a
hand-off is a row, with side A in the A column and side B in the B column, in the classes and
with the exit codes of the front-door compare: exit 1 when a pair is a `regression`, which is
side A passing at least once and side B never.

- It prints each directory's front door and commit. The commit is Prow's `started.json` beside
  the artifact directory, else the first line of the run's `baseline-append.jsonl`.
- It refuses (exit 2) a side A from a run that was not on the platform front door. There the
  default session takes a default-route ask, so the run did not measure the platform agent. Side
  B can come from a run on either front door, because its directive addresses the profile.
- The front door of each directory is the one the front-door compare reads from
  `front-door.txt`, so a directory whose record has no resolved route is refused, whichever side
  it holds.
- It refuses a pair's case with a hand-off in two directories, a directory with no hand-off, and
  a set of directories in which no pair was graded on both sides.
- It adds a note when the two sides ran at different commits, when side A never passed (a row
  of `same` or `better` then shows nothing about the capability), and when a pair ran in none of
  the directories.

It reads the pass counts of a hand-off only. Why a side failed is in the run. No lane calls the
pair mode yet: a person runs it on the artifact directory of a writers run.

## How a move uses a pair

1. Before a capability moves, its side A case is in the nightly and has a record: what the
   platform agent does with the ask today.
2. The pull request that gives a profile the capability adds side B, or turns an existing side B
   green. Side B is that pull request's case under
   [the eval rule](../../.agents/rules/eval_driven_development.md).
3. From then on the compare reads the two records. The flip of a group of work to its profile
   waits for its pairs.

## What a pair does not show

A pair has a model call on each side, so it shows that a capability works. That the narrow permit
refuses what it must refuse has no model call in it, and is a conformance test
(`tests/conformance/`). Scheduled work is not a pair of this kind either: its two sides are the
run that today's schedule starts and the run that an `AgentSchedule` starts
([scheduling-on-profiles.md](scheduling-on-profiles.md)). A pair is graded through the inject
door, which posts to no chat platform, so it shows nothing about chat delivery.

## Done when (proposal for the first comparison of #6539)

- Each group of [the inventory](platform-as-profile-inventory.md) whose work moved to a profile
  has at least one line in `hack/eval/permit-pairs.txt`, or a written reason why a pair cannot
  show it.
- For each line, the two sides were graded on three nights, side A passed on each, and the
  compare exits 0 on each.
- A nightly lane runs the pair mode.
