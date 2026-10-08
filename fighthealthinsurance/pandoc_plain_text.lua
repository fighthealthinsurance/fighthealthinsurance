-- pandoc Lua filter used by utils.pandoc_convert_command for every letter,
-- health-history, cover and transmission-header file rendered to PDF. It keeps
-- an allowlist of plain document structure and turns everything else into the
-- literal characters the author typed, so the PDF engine receives text rather
-- than markup whatever reader built the document (the markdown reader for
-- letters, the html reader for cover pages). This is deny-by-default: a node
-- type nobody listed is rendered as its own text, not passed through. Kept
-- elements carry no identifiers, classes or attributes, so an HTML engine has
-- nothing to act on either. Works with pandoc 2.9 and later; the generic Inline
-- and Block functions it relies on are called for every element in both.

local stringify = pandoc.utils.stringify

-- Inline element tags kept as they are. Emph and Strong carry ordinary letter
-- emphasis; Quoted keeps the quotation marks around quoted text; a footnote
-- (Note) keeps its blocks, which pass through Block below like any others.
local allowed_inlines = {
  Str = true,
  Space = true,
  SoftBreak = true,
  LineBreak = true,
  Emph = true,
  Strong = true,
  Quoted = true,
  Note = true,
}

-- Block element tags kept as they are: paragraphs, lists, definition lists,
-- the line-block used for addresses, and a horizontal rule. Headings and
-- tables are kept too, rebuilt without their attributes (see Block).
local allowed_blocks = {
  Para = true,
  Plain = true,
  BulletList = true,
  OrderedList = true,
  DefinitionList = true,
  LineBlock = true,
  HorizontalRule = true,
}

-- One line of text as a Str. A blank or space-only line becomes a single
-- non-breaking space, so a line break never opens a paragraph (the LaTeX
-- writer would start the paragraph with \\, which every engine rejects).
local function line_str(line)
  if line:match("^%s*$") then
    return pandoc.Str("\194\160")
  end
  return pandoc.Str(line)
end

-- Turn one text string into inlines, keeping its line breaks so the text of a
-- code span or math still reads as separate lines. Blank lines at either end
-- are dropped; text with no visible characters gives no inlines at all.
local function text_to_inlines(text)
  text = text:gsub("^%s*\n", ""):gsub("\n%s*$", "")
  if text:match("^%s*$") then
    return {}
  end
  local inlines = {}
  local first = true
  for line in (text .. "\n"):gmatch("(.-)\n") do
    if not first then
      table.insert(inlines, pandoc.LineBreak())
    end
    first = false
    table.insert(inlines, line_str(line))
  end
  return inlines
end

function Inline(el)
  if allowed_inlines[el.t] then
    return nil
  end
  -- Raw markup is dropped, not printed.
  if el.t == "RawInline" then
    return {}
  end
  -- Span, Link, Image, Cite, SmallCaps and the like hold inline content: keep
  -- the characters, drop the markup, the link target and the image source (so
  -- no file or address is fetched while the PDF is built).
  if el.content ~= nil then
    return el.content
  end
  -- Code, Math and anything else leaf-like: keep the characters.
  return text_to_inlines(stringify(el))
end

local function code_block_lines(text)
  local lines = {}
  for line in (text .. "\n"):gmatch("(.-)\n") do
    table.insert(lines, { line_str(line) })
  end
  return lines
end

-- A table keeps its rows and cells but none of the attributes that pandoc
-- 2.10 and later attach to the table, its rows and its cells; rebuilding it
-- through pandoc's simple-table form leaves them all empty. pandoc 2.9 tables
-- carry no attributes, so they are kept as they are. A table this filter
-- cannot rebuild falls back to its text.
local function plain_table(el)
  if pandoc.utils.to_simple_table ~= nil then
    return pandoc.utils.from_simple_table(pandoc.utils.to_simple_table(el))
  end
  if el.attr == nil then
    return el
  end
  return nil
end

function Block(el)
  -- A heading keeps its level and text, not its identifier or attributes.
  if el.t == "Header" then
    return pandoc.Header(el.level, el.content)
  end
  if el.t == "Table" then
    local kept = plain_table(el)
    if kept ~= nil then
      return kept
    end
  end
  if allowed_blocks[el.t] then
    return nil
  end
  -- Keep the content, drop the wrapper and its attributes; for a blockquote
  -- this also flattens the nesting, so the engine receives a plain series of
  -- paragraphs however deeply the quote was nested.
  if el.t == "Div" or el.t == "BlockQuote" then
    return el.content
  end
  -- A code block keeps its lines, each as plain text.
  if el.t == "CodeBlock" then
    return pandoc.LineBlock(code_block_lines(el.text))
  end
  -- Raw markup is dropped, not printed.
  if el.t == "RawBlock" then
    return {}
  end
  -- Anything else: keep its characters as a paragraph.
  local inlines = text_to_inlines(stringify(el))
  if #inlines == 0 then
    return {}
  end
  return pandoc.Para(inlines)
end
