# Ask your documents

Questions are answered from an indexed corpus, with `[n]` markers pointing back
to the source of each claim.

Two things to watch, because they are the point:

- **Expand a step** to see which passages were retrieved and how they scored.
  Nothing is answered from memory — if it was not retrieved, it is not claimed.
- **Ask something the corpus does not cover.** The expected answer is "I don't
  know", not a plausible guess. That behaviour is measured as *refusal accuracy*
  in the eval suite.
