# LLM foundations, from weights to tokens/sec

Not a module doc. This is the high-level mental model behind Module 4's rating layer and
Appendix A's economics, written for interview recall. Every section ends in something you
can say out loud. The through-line is that an LLM is a big pile of numbers, answering is
arithmetic against those numbers, and speed is set by how fast the numbers can be fed to
the arithmetic.

## 1. Weights, and training vs inference

A model IS a file of numbers. The ~3GB that `moon run root:ollama-pull` downloads is billions of
decimal numbers and almost nothing else. Each number is a **weight**, one tiny dial that
answers "how much should this input influence that output." Nobody sets the dials by hand.
qwen3:4b means 4 billion dials.

**Training** is build time. The lab shows the model trillions of examples and nudges the
dials after each one until outputs look right. Insanely expensive, done once, produces the
artifact.

**Inference** is runtime. The dials are frozen, read-only. Text in, arithmetic against the
frozen weights, text out. Nothing is learned during inference. When our `rate_signal` task
POSTs a caption to `/chat/completions`, that's inference, a pure function of
(weights, input).

The Docker parallel is exact:

```
  docker build  ─────────▶  image (frozen artifact)  ─────────▶  docker run
   expensive,                 pull it once,                       cheap, repeatable,
   done at the lab            it never changes                    every request

  TRAINING      ─────────▶  weights file             ─────────▶  INFERENCE
   adjusts the dials          ~3GB of numbers                     reads the dials,
   (millions of $)            (qwen3:4b = 4B dials)               never writes them
```

> **Soundbite.** "Training writes the weights, inference reads them. The model file is a
> frozen artifact, like a Docker image. Serving a model is `docker run`, not
> `docker build`."

## 2. Text in, next token out (the whole pipeline)

Models don't read letters or words. Text gets chopped into **tokens**, chunks from a fixed
menu of ~100k entries, each about 3/4 of a word ("influencer" might be `influ` + `encer`).
Why chunks from a menu? Because math needs numbers, not strings, and a fixed menu turns any
text into a list of integers.

One full pass through the model produces exactly ONE next token. Then it loops.

```
  "rate this caption"
        │
        │  (1) TOKENIZE  chop into menu chunks, swap each for its menu number
        ▼
  [ 8934, 419, 24710 ]                          token ids (just integers)
        │
        │  (2) EMBED  swap each id for its vector, a list of ~4096 numbers
        │      that act as the token's coordinates in meaning-space
        ▼
  [ [0.12, -0.4, ...], [1.7, 0.3, ...], ... ]   one vector per token
        │
        │  (3) LAYERS  push the vectors through weight matrix after
        │      weight matrix. one layer = one giant multiply-and-add
        ▼
     ┌─────────── layer 1:  vectors × weight-matrix ───────────┐
     ├─────────── layer 2:  vectors × weight-matrix ───────────┤
     │                      ... ~30 more ...                   │
     └─────────── layer N:  vectors × weight-matrix ───────────┘
        │
        │  (4) SCORE  the final vector becomes one score for EVERY
        │      entry in the 100k menu
        ▼
     "the": 0.1   " 0": 11.9   " relevance": 9.4   ...
        │
        │  (5) PICK the winner. that's the next token.
        ▼
     append it to the input and go back to (3) for the token after it
```

A **layer** is nothing exotic. It's one big grid of weights, and pushing a vector through
it means each output number is a weighted sum of all the input numbers (multiply each
input by a weight, add them up). That multiply-then-add, repeated billions of times, is
the entire computation. There's no other operation hiding inside.

**Scoring** is the same trick one last time. The final layer's output gets multiplied
against one more weight matrix whose output is 100k numbers, one per menu entry. Highest
number wins, that's the next token. The loop is strictly sequential (token 13's input
includes token 12), which is why answers stream word by word and why you can't
parallelize your way out of a slow model.

> **Soundbite.** "Text becomes token ids, ids become vectors, vectors get multiplied
> through a few dozen weight matrices, and the output is a score for every token in the
> vocabulary. Pick the top one, append, repeat. One full pass per token."

## 3. Order matters, so where does it live?

Fair question, because step (1) looks like it produces a bag of ids. If the model only saw
the bag, "dog bites man" and "man bites dog" would be identical input.

```
  without position stamps               with position stamps
  ─────────────────────────             ─────────────────────────
  "dog bites man"  → {dog,bites,man}    [dog @1] [bites @2] [man @3]
  "man bites dog"  → {dog,bites,man}    [man @1] [bites @2] [dog @3]
        same bag, order lost                different inputs, order kept
```

The fix happens at step (2). Before the vectors enter layer 1, each token's vector gets
its **position mixed in**, position 1 has its own numeric pattern, position 2 another, and
that pattern is added to the token's vector. So "dog at position 1" and "dog at position 3"
are literally different lists of numbers by the time the layers see them. Order isn't
tracked beside the data, it's baked into the data, the way each of our signals carries its
own `captured_at` instead of relying on arrival order.

## 4. Disk vs memory vs compute (the part that decides speed)

Three physical places, three different jobs. None of them is "the decisions", the
decisions ARE arithmetic, and arithmetic only happens in one of the three.

```
   DISK                      RAM / VRAM                     CORES ("compute")
   where the file            where the weights              where math happens.
   sleeps when nothing       sit while serving.             a weight must physically
   is running.               holding, not doing.            arrive here to be
                                                            multiplied.
  ┌──────────────┐   read   ┌──────────────────┐  stream   ┌──────────────────┐
  │  model file  │  ONCE,   │  all 4B weights  │  EVERY    │   multiply, add  │
  │  qwen3 ~3GB  │ ───────▶ │  loaded, ready   │ ════════▶ │   multiply, add  │
  └──────────────┘  at      └──────────────────┘  token    └──────────────────┘
                    startup
                              the width of ════▶ is MEMORY BANDWIDTH,
                              bytes per second flowing from RAM into the cores
```

Pin down the two words that stay fuzzy:

- **Compute** is the *doing*. How many multiply-adds per second the cores can execute
  (measured in FLOPs). Think of it as the chef's chopping speed.
- **Memory** is the *holding*, and memory **bandwidth** is the *feeding rate*, how fast
  ingredients move from the pantry (RAM) to the chef's counter (cores). It is NOT disk.
  Disk is the delivery truck that stocked the pantry once, at startup, and then went home.

The crunch, and the whole reason this section exists. To produce ONE token, essentially
every weight must travel from RAM into the cores (step 3 above uses every layer's matrix,
every pass). Modern cores can do the math far faster than the pipe can deliver the
numbers. So the cores idle, starved, and the pipe sets the speed. You already know this
failure shape from Postgres, a seq scan is disk-bound while the CPU sits bored. Same
shape, one storage tier up.

That gives a formula you can do in your head:

```
  tokens/sec  ≈  memory bandwidth / model bytes

  laptop CPU + RAM      ~100 GB/s  /  3 GB   ≈  ~30 tok/s ceiling (reality: less)
  NVIDIA H100 + HBM   ~3,000 GB/s  /  3 GB   ≈  ~1,000 tok/s ceiling
```

Same model, same math, ~30x difference, purely because the pipe is fatter.

> **Soundbite.** "Inference is memory-bandwidth bound, not compute bound. Every token
> requires streaming every weight from memory into the cores, so throughput is roughly
> bandwidth divided by model size. Disk isn't in the loop at all after startup."

## 5. CPU vs GPU, and why NVIDIA

```
          CPU                                    GPU
  ┌─────────────────────┐          ┌────────────────────────────────┐
  │  ▓▓▓▓   ▓▓▓▓        │          │  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
  │  ▓▓▓▓   ▓▓▓▓  ×8    │          │  ░░░░░░░░░░ ×10,000+ ░░░░░░░░  │
  │                     │          │  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
  │  a few SMART cores  │          │  thousands of SIMPLE cores     │
  │  branching, logic,  │          │  all doing the SAME multiply-  │
  │  general purpose    │          │  add, in lockstep              │
  └─────────────────────┘          └────────────────────────────────┘
     runs your API                    a matmul appliance
```

Matrix multiply is embarrassingly parallel. Each output number is an independent weighted
sum, none waits on another, same reason our fan-out scrapes five influencers concurrently.
A GPU is that insight cast into silicon, plus one more trick, its memory (**HBM**, high
bandwidth memory) is stacked physically next to the cores, which is where the 3,000 GB/s
pipe from section 4 comes from.

NVIDIA's lead is three stacked moats. The most parallel cores, the fattest pipe (HBM), and
**CUDA**, the software layer that virtually all ML code is written against, so even a
rival chip with great hardware starts with an ecosystem of zero. When people say "GPU
shortage" they mean "not enough matmul appliances with fat pipes."

## 6. Quantization, in one breath

Store each weight in fewer bits.

```
  16-bit weight:   0100101011010010    (2 bytes each  →  8 GB model)
   4-bit weight:   0101                (0.5 bytes each → 2 GB model)

  4x smaller model  →  4x fewer bytes down the pipe per token
                    →  ~4x more tokens/sec, slightly blurrier dials
```

It's lossy compression on the weights, and by section 4's formula, compression IS speed.
The qwen3:4b that Ollama serves is already quantized, which is the only reason a 4B model
is pleasant on a laptop CPU.

## 7. The interview version, assembled

Every clause below is one section of this doc:

> "LLM inference is a sequential loop of giant matrix multiplies, one full pass per token,
> and each pass streams every weight from memory into the chip. So throughput is roughly
> memory bandwidth divided by model size. That's why GPUs with HBM dominate, why
> quantization speeds things up (fewer bytes per weight, fewer bytes down the pipe), and
> why CPU-only hosting like Railway gives you single-digit tokens per second."

```
  "sequential loop ... one pass per token"      section 2  (the token loop)
  "giant matrix multiplies"                     sections 1-2  (layers = weight matrices)
  "streams every weight from memory"            section 4  (the pipe, not disk)
  "bandwidth divided by model size"             section 4  (the formula)
  "GPUs with HBM dominate"                      section 5  (fat pipe + parallel cores)
  "quantization ... fewer bytes down the pipe"  section 6
  "CPU-only hosting ... single-digit tok/s"     Appendix A's entire thesis
```

That last clause is why Appendix A (self-hosting Ollama on Railway) is a parked experiment
and not the plan. Railway has no GPUs, so the pipe is a CPU's RAM bus, and always-on RAM
for the weights bills more per month than the API pennies it replaces. Own the interface,
rent the model.
