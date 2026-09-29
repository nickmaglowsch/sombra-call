# Trigger detection (G1–G5)

`sombra.trigger.NameTriggerDetector` implements `contracts.TriggerDetector`. It sees
every timeline entry and returns a `TriggerEvent` when someone in the call (`OUTROS`)
asks the user something. It is rule-based and runs locally; the phase-3 classifier
(G6) will replace or complement it.

## Construction

```python
from sombra.trigger import NameTriggerDetector, TriggerSettings

detector = NameTriggerDetector("Nick", aliases=["Nicolas"], settings=TriggerSettings())
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `threshold` | 0.7 | minimum `score` to fire |
| `min_request` | 0.4 | request structure needed at all; a name alone never fires |
| `window_s` | 60 | G3 context window returned in `TriggerEvent.window` |
| `split_s` | 5 | max gap between a name line and its question line |
| `cooldown_s` | 30 | G5: no new trigger this soon after the last one |
| `dedupe_s` / `dedupe_ratio` | 120 / 90 | G5: a question this similar (rapidfuzz ratio) to one fired within `dedupe_s` is dropped |
| `w_name` / `w_vocative` / `w_request` | 0.45 / 0.25 / 0.30 | score weights |

## Rules

1. **Only `OUTROS`.** `EU` lines are buffered for the window but never fire.
2. **Name (G1).** Each word is reduced to a phonetic key (accents and case folded,
   `ck/qu/c→k`, `ph→f`, `th→t`, `z→s`, `y→i`, doubled letters collapsed, final
   `e`/`i` after a consonant dropped), so "Nick", "Nik", "Nic", "Nique", "Nicky" all
   become `nik`. Keys of 4+ letters also match within edit distance 1 (2 for 7+
   letters), but only on capitalised words with the same first letter. Multi-word
   aliases ("Ana Paula") are supported.
3. **Position (G2).** The name must look like a call: sentence start, end, or
   between commas, optionally after an interjection ("e aí Nick, …"). After an article
   or preposition ("o Nick", "pro Nick") or before a 3rd-person verb without a comma
   ("Nick falou que …") it is a reference and does not fire. The same holds for a
   statement about the user whose only question is a trailing tag ("Nick tá de férias,
   né?", "Nick fechou com o cliente, não foi?"), and for another person's full name
   (a capitalised word right after the name that is not part of a configured alias:
   "Nicolas Cage …"). With a comma ("Nick, tá de férias?") it is a call. When the
   name opens the line and "você/cê/tu" follows directly, a dropped `?` is assumed
   ("nick você já testou isso em produção").
4. **Request (G2).** Question mark, interrogatives, opinion asks ("você acha"),
   request verbs ("pode", "consegue", "explica", "me diz"), turn handoffs ("sua vez",
   "contigo"). "Você" and weak verbs only count inside a question. Closings
   ("valeu", "parabéns") lower the score. Bare greetings and line checks
   ("Nick, tudo bem?", "tá me ouvindo?") do not fire; "Nick, e aí?" does (it hands
   over the turn).
5. **Split questions.** A bare call ("Nick…") followed within `split_s` by a
   question, or a question followed by a bare call ("…? Nick?"), fire as one; the
   question is both lines joined. A follow-up that calls someone else ("Maria, …",
   "Pessoal, …") does not.
6. **Screen (G4).** Deictics ("aqui", "tá vendo", "esse gráfico", "essa planilha",
   "nessa tela", "this chart", …) set `needs_screen`; `candidate_frames` is then the
   last frame marker seen plus up to 2 earlier distinct ones, current first. When the
   screen does not matter, `candidate_frames` is empty.

## Corpus and evaluation

`tests/fixtures/trigger/corpus.jsonl`: hand-written, synthetic PT-BR snippets
(standup, planning, demo, incident speech) in `transcript.md` line format, each with
the expected outcome. The `dev` split was used while writing the rules; the
`holdout` split (~30%) was written separately. `tests/trigger/test_trigger_corpus.py`
asserts precision and recall ≥ 0.9 on each split. The evaluation helper is
`sombra.trigger.evaluate(load_corpus(path))`.

Known misses on the holdout split, kept on purpose so the numbers stay honest:

- `hm08` "Nico, …": a 3-letter key only matches exactly, so "Nico" is not "Nick".
- `hn08` "Nick, você manda muito bem nisso.": "manda" is read as a request verb.
- `hw04` "tá mas nick e o custo disso": no punctuation and no request word at all.
