# Acroformer

Turn flat (non-fillable) PDF forms into fillable **AcroForms** — automatically.

Many institutional forms (bank account openings, KYC packets, government forms)
are distributed as flat PDFs: the boxes are printed, but nothing is fillable.
`acroformer` detects the drawn form geometry and overlays real AcroForm widgets
on the original pages, so the output looks 100% identical to the source and can
be filled in Adobe Acrobat/Reader, macOS Preview, Chrome, or programmatically
(PyMuPDF, pypdf, pdftk...).

## What it detects

| Printed geometry | Becomes |
|---|---|
| Rows of character boxes (`|_|_|_|...`) | one text field — a **comb** field (one char per box) for short runs like dates/postal codes, a free-flowing text field for long runs like names |
| Small square boxes with a label to the right | checkbox |
| Single boxes flanked by printed `/` or `-` (dd / mm / yyyy) | separate text boxes named `..._dd`, `..._mm`, `..._yyyy` |
| Dotted leaders (`.......`) and underscore runs | text field |
| Drawn write-on lines (`Lainnya/Others ____`) | text field |
| Empty bordered table cells and ruled table rows | text field |

Heuristics that keep the output clean: checkboxes containing pre-printed text
are skipped, fields inside shaded header cells (bank-use areas) are suppressed,
and overlapping candidates are deduplicated.

## Field naming

Fields are named from the printed label next to them, so your integration code
stays readable:

```
p02_nama_depan
p02_tempat_tanggal_lahir_dd
p02_tempat_tanggal_lahir_mm
p02_tempat_tanggal_lahir_yyyy
p02_alamat_sesuai_ktp
p02_alamat_sesuai_ktp_2      <- continuation line inherits the label
p02_npwp_wajib_diisi
```

Checkboxes take the label to their right (`p02_saham`), text fields the label
to their left (falling back to the label line above). Bilingual labels
("Nama Depan / First Name") keep the first part. The `pNN_` page prefix
guarantees global uniqueness, duplicates get `_2`, `_3`, ...

## Install

```bash
pip install pymupdf
```

## Usage

```bash
python acroformer.py input.pdf                       # writes input_acroform.pdf
python acroformer.py input.pdf -o filled_form.pdf
python acroformer.py input.pdf --map fields.json     # JSON list of all fields
python acroformer.py input.pdf --debug-overlay check.pdf
```

`--map` writes `[{name, page, type, maxlen, rect}, ...]` — handy for building
auto-fill integrations.

`--debug-overlay` writes a copy with color-coded outlines (red = text,
blue = comb, green = checkbox) so you can eyeball the detection page by page.

### Filling programmatically

```python
import fitz  # pymupdf

doc = fitz.open("input_acroform.pdf")
for page in doc:
    for w in page.widgets():
        if w.field_name == "p02_nama_depan":
            w.field_value = "FURY"
            w.update()
doc.save("filled.pdf")
```

## How it works

The detector reads the page's vector drawings (`page.get_drawings()`) and text
(`page.get_text("rawdict")`), then classifies geometry:

1. **Vertical tick marks** are clustered into rows and split into runs at
   larger gaps and at printed glyphs (so `dd / mm / yyyy` splits into three).
2. Runs of 3+ evenly spaced ticks become comb/text fields; tick pairs are
   disambiguated by context — a `/` or `-` neighbor means a date cell,
   an alphanumeric label immediately right means a checkbox. (On many forms
   a checkbox and a 2-digit date box are geometrically identical.)
3. Dotted leaders and underscores are found per character, so labels and
   fillers mixed in one text span still work.
4. Everything is deduplicated, and widgets are added on top of the original,
   untouched pages.

Tested on a 67-page Indonesian securities + bank account opening packet
(667 fields detected); heuristics are generic, not form-specific.

## Limitations

- Wet-signature areas are intentionally left without fields.
- Radio-button semantics are not inferred — exclusive choices are produced as
  independent checkboxes.
- Detection is heuristic; always eyeball `--debug-overlay` on a new form.

## License

MIT
