-- pandoc Lua filter used by utils.pandoc_convert_command for every document
-- rendered to PDF. Math elements and raw TeX/LaTeX elements become plain text
-- holding the characters they contain, so the PDF engine receives text rather
-- than markup whatever reader built the document (the markdown reader for
-- letters, the html reader for cover pages, which turns
-- <script type="math/tex"> and MathML into math). Works with pandoc 2.9 and
-- later.

local tex_formats = { tex = true, latex = true, beamer = true, context = true }

-- pandoc compares raw formats without regard to case.
local function is_tex(format)
  return tex_formats[string.lower(format)] == true
end

function Math(el)
  return pandoc.Str(el.text)
end

function RawInline(el)
  if is_tex(el.format) then
    return pandoc.Str(el.text)
  end
end

function RawBlock(el)
  if is_tex(el.format) then
    return pandoc.Para({ pandoc.Str(el.text) })
  end
end
