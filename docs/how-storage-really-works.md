# How storage really works (bits, pages, disk order, B-trees)

The first-principles layer under Module 1. Everything the EXPLAIN drills show (page
counts, index scans, bitmap scans, partition pruning) reduces to a handful of physical
facts about how bits sit on hardware and how they move between disk and memory. This doc
is the mechanism all the way down, written for someone strong on infra (S3, CDNs,
stateless containers) but new to the storage/memory vocabulary.

Companion diagrams already in `docs/`. [pages-disk-memory.svg](pages-disk-memory.svg)
(a table is pages on disk, a few copied into memory) and
[latency-ladder.svg](latency-ladder.svg) (the storage tiers at human scale).

## What a bit physically is

A bit is one physical thing in one of two states. What the *thing* is depends on the medium.

- **RAM**: a tiny capacitor holding a charge. Charged = 1, drained = 0. The charge leaks,
  so RAM is constantly refreshed and dies the instant power stops. That's why memory only
  lasts as long as the program runs. The charge literally drains away.
- **SSD (flash)**: electrons trapped in an insulated cell. The insulation holds them with
  no power, so it persists. That's disk.
- **Spinning hard drive**: a spot of magnetic material polarized north or south on a metal
  platter. Polarity persists with no power too.

So "ones and zeros" is not a metaphor. It's charge, or trapped electrons, or magnetic
direction. A byte is 8 of those in a row. An 8KB page is ~8000 of those bytes in a row.

## What "order" means physically, and why disk has it and RAM doesn't

The three media differ in how you reach a specific bit, and that difference is the whole story.

**RAM is actually random-access** (the "R" in RAM). A wire-grid address decoder makes bit
number 5 and bit number 5,000,000 cost the same to reach. Put the address on the bus, the
charge comes back. There's no notion of "order" because every location is equidistant.
This is why the intuition "items in memory you just grab" is correct, for RAM.

**A spinning disk is a record player.** One physical head on an arm, one platter spinning
underneath. To read a byte the arm swings to the right track, then waits for that byte to
rotate under the head. That mechanical movement is a **seek**, the ~10 millisecond rung on
the latency ladder.

The punchline. If the next byte you want is right after the last one on the same track, the
head doesn't move, the platter just keeps spinning and bytes stream under it. That's
**sequential** order, nearly free. If the next byte is on a far track, the arm swings
again, another full seek. That's **random** order, and you pay the 10ms every jump.

```
  SEQUENTIAL: head parks once, bytes stream under it
     head v
     +----------------------------------+
     | ##############################   |   one seek, then free
     +----------------------------------+

  RANDOM: head swings back and forth for each read
     head v        v      v          v
     +----------------------------------+
     | ##    ##        ##       ##       |  a full seek every time
     +----------------------------------+
```

That physical asymmetry (seek is expensive, streaming is cheap) is where "disk order"
comes from, and it's baked into how the database plans queries even today.

## Why pages exist

Because a seek is so expensive relative to reading, paying a full seek to fetch one byte
would be insane. So the moment the head is there, you grab a big chunk while you can, the
whole 8KB page. The fixed cost of getting there gets amortized over ~8000 bytes instead of 1.

That's the real reason pages exist, and the reason `Buffers` counts pages, not bytes. The
page is the smallest amount worth the trip. Same logic as loading a shipping container
instead of driving to the port for one item. The drive to the port (the seek) dominates,
so you fill the container.

## The bitmap scan is Postgres being explicit about disk order

A bitmap is literally a row of on/off bits, one per page, where 1 means "this page has at
least one match" and 0 means "skip it." For a 10-page partition:

```
  page:   1  2  3  4  5  6  7  8  9  10
  bit:    0  1  0  0  1  1  0  0  0  1
                +- pages 2, 5, 6, 10 have matches; the rest don't
```

A bit is the smallest unit a computer has, so a bitmap for thousands of pages is tiny and
lives in memory. The bitmap scan splits the read into two steps to control disk order.

```
  step 1  walk the index, flip a bit for every page that has a match
          -> Bitmap Index Scan   (builds the 0/1 row above)

  step 2  read the flagged heap pages in PHYSICAL order, front to back, no backtracking
          -> Bitmap Heap Scan    (reads the actual rows)
```

The win is disk order:

```
  index gives matches in KEY order:   page 10, 2, 6, 5   (head bounces)
  bitmap sorts them into DISK order:  page 2, 5, 6, 10   (head sweeps once)
```

Same rows, far fewer seeks. That's why it's the middle gear between a plain index scan
(reads few pages, but in scattered key order) and a seq scan (reads in disk order, but
reads every page). Fewer pages than a seq scan, smoother reads than an index scan.

The `Recheck Cond` in the plan is the catch. If a page has so many matches the bitmap runs
low on memory, it degrades to "this page *might* have matches," so the heap scan re-checks
the rows against the condition. On tiny data it's just belt-and-suspenders.

One-line version for an interview. *A bitmap scan uses the index to decide which pages to
read, then reads them sequentially, giving you index selectivity with sequential-read speed.*

## The B-tree index persists on disk, exactly like the table

The index is not a special in-memory structure. It's stored on disk as its own set of 8KB
pages, in its own file, next to the table's file. Everything is pages, all the way down.
The table is heap pages, the index is index pages, same 8KB unit.

The tree's nodes (root, internal, leaf) are each just pages on disk. The subtle bit that
makes it work: pointers between nodes are **page numbers, not memory addresses**. A parent
says "my child is block 4207," not "my child is at RAM address 0x7f3a." Page numbers are
stable on disk, so the tree survives a restart intact. To follow a pointer, Postgres loads
that page number into the buffer cache and reads it there.

```
  DISK (persistent)                 RAM / buffer cache (fast, evictable)
  +--------------------+            +--------------------+
  | index file:        |  first     | root page  (hot,   |
  |  [root page]       |  touch     |   never evicted)   |
  |  [internal pages]  |  ------>    | a leaf page or two |
  |  [leaf pages]      |  pulls      |                    |
  | heap file:         |  pages in   | a few heap pages   |
  |  [data pages]      |            +--------------------+
  +--------------------+
```

On startup nothing is loaded. Pages get pulled into the buffer cache the first time they're
touched, evicted later if cold. The root and upper levels stay permanently hot, because
every lookup touches the root, so it's always cached. The leaves come and go.

Writes go the other way. An `INSERT` modifies the relevant index page **in RAM** (marks it
"dirty"), writes a WAL record so the change is durable even before the page hits disk, and
a background process flushes the dirty page to disk later. Crash mid-flush, replay the WAL,
the tree's fine. That WAL is the "what if the machine dies mid-write" answer.

This closes the loop on drill (a)'s `Buffers: shared hit=5`. That five was index pages plus
heap pages together, walking down the B-tree *and* fetching the matching rows, all counted
in the same page currency. The index isn't free to read, just cheap, because the tree is
shallow so it's only a few pages down to the leaf.

## The one paragraph that ties it together

Bits are charge or trapped electrons or magnetism. RAM is equidistant so it has no order.
Disk has a physical head, so adjacency is nearly free and jumping is expensive. Pages exist
to amortize the expensive jump, which is why cost is measured in pages. Indexes and the
heap both live on disk as pages and get loaded into the buffer cache on demand, with hot
pages (like the B-tree root) staying resident. Durability comes from WAL, written before
the page itself is flushed. Every node type in an EXPLAIN plan (seq scan, index scan,
bitmap scan, index-only scan) is just a different strategy for moving the fewest pages
between disk and memory. That's the whole of Module 1's physical layer.
