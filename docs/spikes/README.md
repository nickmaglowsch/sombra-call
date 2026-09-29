# Spike reports

One file per spike issue: `NNN-short-name.md` where `NNN` is the issue number. Each report answers the spike's question with a yes/no and numbers, and lists:

1. **Question** and the pass criterion from the issue.
2. **Setup**: hardware (chip/CPU/GPU, RAM), OS and desktop version, library versions.
3. **Method**: exact commands or the script under `scripts/spikes/` that reproduces it.
4. **Results**: a table of measured numbers.
5. **Decision**: what the project does now, and any ADR it produced.

Throwaway code goes under `scripts/spikes/<issue>/` and is exempt from coverage, but must pass lint.
