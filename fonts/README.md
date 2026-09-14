# PDF fonts

ReportLab's built-in fonts are Latin-1 only, so the rupee sign (₹, U+20B9)
prints as an empty box. The PDF module looks for a Unicode TTF here first:

```
fonts/body.ttf        # regular
fonts/body-bold.ttf   # bold (optional)
```

Any TTF containing U+20B9 works — Noto Sans, DejaVu Sans 2.35+, Inter,
Plus Jakarta Sans. Drop the files in and restart; the module checks the font's
character map and switches to `₹` automatically.

Without one, amounts print as `Rs.1,23,456.50`, which is correct and never
renders as tofu. On Linux (including Render) DejaVu is usually already present
at `/usr/share/fonts/truetype/dejavu/` and is picked up without any setup.

These files are deliberately not committed — fonts carry their own licences.
