---
paths:
  - 'style_guides/**'
  - 'style_guides.py'
  - 'scripts/extract_style_guides.py'
  - 'gemini_client.py'
---

# Style guides and the variant split

History: `C:/Users/kumar/Downloads/README.md` (local, not in the repo) §4.3, §4.4, §4.7, §6.

## Who decides what

- The **user-selected variant alone** (British/American) decides: spelling variant (-ise/-ize,
  -our/-or…), the quote-mark family, punctuation placement against the closing quote, and preterite
  forms (BrE dreamt/learnt/burnt vs AmE -ed). These rules live **only in the system prompt**.
- The **style guide** decides everything else: numbers, dates, citations, capitalisation of terms,
  abbreviations, italics, hyphenation and usage.
- So the style guide `.txt` files must never state a spelling variant or a quote-mark rule. If a
  client asks for one, it goes in the prompt and depends on the variant.

## The .txt files

- `style_guides/*.txt` are the source of truth. They were extracted from the PDFs once and then
  edited by hand (noise removed, variant declarations stripped, precedence notes added).
  **Never regenerate them from the PDFs.** That would undo the edits.
- They are sent with every call, so their length is a cost on every request. Keep edits concise.
- To add a guide, add the `.txt` and register it in `config.STYLE_GUIDES` (`label`, `path`).
  `style_guides.load_style_guides` caches them.

## Spelling

Never add a dictionary-based BrE/AmE pass (it produced filter→philtre, draft→draught). Word choice
needs sentence context, so it stays with the model.
