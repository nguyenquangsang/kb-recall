# Claude Code truncates MCP tool descriptions at ~2,100 characters — silently

If you maintain an MCP server, the docstring on each tool is not documentation. It is the *entire* interface the model ever sees. There is no separate schema description, no README the model reads at call time — the docstring is it.

I built an MCP server with eight tools and wrote long, careful docstrings for each. Then I noticed the model behaving as though it had never read the second half of them. It hadn't.

## The finding

Claude Code defers MCP tools behind `ToolSearch`. When a deferred tool's description is rendered, it is cut at roughly **2,000–2,150 characters** — and the cut is silent. No error, no warning, no ellipsis, no truncated-output flag. The tool registers fine and the call works fine. You only find out by measuring.

Two independent measurements on the same server put the ceiling at **2011–2154** and **2100–2154** characters.

## The cut is proportional, not fixed

The more interesting result is what happens as the docstring grows. The truncation does not clip a fixed amount — it clips *down to* the ceiling, so the fraction lost grows with length.

| Docstring length | Rendered | Fraction kept |
|---|---|---|
| 3,933 chars | ~2,011 | **~51%** |
| 7,989 chars | ~2,154 | **~27%** |

Both rows imply a ceiling in the same 2,000–2,150 window. That agreement across two very different input lengths is what makes this a measurement rather than an anecdote.

The practical consequence is perverse: **writing a more thorough docstring can make the model see proportionally less of it.** Effort spent past roughly 2,000 characters is not merely wasted — it dilutes the part that survives.

## Why this is worse than it looks

A silent cut is bad on its own. Two details make it worse.

**The JSON schema carries no per-parameter description.** Inspecting a deferred tool's schema shows `parameters.properties` with only `type`, `title`, and `default`. If your docstring has prose per parameter — which is the normal place to put it — that prose reaches the model *only* through the docstring text. Cut before your parameter documentation and the model never learns what the parameter means. It will pass something plausible.

**The cut is positioned, not summarized.** There is no attempt to keep the important parts. Whatever falls past the ceiling is simply gone, in document order.

In my case the casualty was `Args:` — a block that sat near the end of several docstrings, after the output-guidance and examples. So the model could see *what the tool did* and *how to call it in general*, but not *what each argument meant*.

## What I changed

Three things, in order of how much they helped.

**Move `Args:` immediately after the format section.** Not last. This is the load-bearing fix, because parameter documentation is the one thing the JSON schema cannot recover. Measure the length *through* the end of `Args:` — not the total — and keep that number comfortably below ~1,900.

**Move post-call guidance to a response footer.** Most docstrings end with "after this returns, do X" instructions. That content is acted on *after* the call, so it can be appended to the tool's return value instead of living in the docstring — and the return value is not subject to the ceiling at all. This buys back several hundred characters of budget without deleting anything.

Test for what can move: can this rule only be acted on using the tool's actual return data? If yes, it is footer-safe even if it looks redundant. If it is pre-call decision guidance — when to call, how to fill arguments — it is not, no matter how duplicated it looks elsewhere.

**Measure after every edit.** This is the one I would not skip. Small restorations drift back into the danger zone one edit at a time, and nothing warns you.

```bash
python -c "
import inspect
from mypackage import server
d = inspect.getdoc(server.my_tool)
print('total:', len(d), '| through Args:', d.find('Args:'))
"
```

Track the second number. Total length alone no longer tells you whether the load-bearing content survives — after trimming, several of my tools still exceed 2,100 characters in total while every one has its `Args:` block safely under 1,900.

## How this was measured

A note on method, because it should change how much weight you put on the exact numbers.

This was measured by probing a live session — registering the server in Claude Code, exercising the deferred tools, and reading back from the session where the description stopped appearing. It is a behavioral observation, not an instrumented one. Two independent passes on separate occasions gave ceilings of 2011–2154 and 2100–2154 characters.

Being candid about what that does and does not establish:

- **It establishes the ceiling.** Two passes, two very different docstring lengths, both landing in the same narrow window — and the kept-fractions (51% and 27%) cross-check arithmetically against it. That mutual agreement is the actual evidence.
- **It does not establish that the ceiling is stable.** Not across Claude Code versions, not across model families, not across how many tools are deferred in a session. The mechanism behind the cut is not observable from outside the client, so I make no claim about why it lands where it does.

To confirm independently, the session transcript records what the model was actually served. `~/.claude/projects/*/*.jsonl` contains the tool-search results, so the rendered description can be compared directly against the docstring on disk — a more rigorous check than the one that produced these numbers.

For reference, the docstrings on that server have since been trimmed: the 3,933- and 7,989-character figures are lengths *at the time of measurement*, not current ones.

---

*Measured on Claude Code against an eight-tool MCP server, September 2026. The ceiling may move; the failure mode — silent, proportional, and invisible to the schema — is the part worth designing around regardless of the exact number.*
