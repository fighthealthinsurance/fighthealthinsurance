# Blog Post Creation Guide

This document outlines the standardized process and format for creating blog posts in the Fight Health Insurance project.

## File Structure and Location

- **Directory**: `/fighthealthinsurance/static/blog/`
- **Naming Convention**: `YYMMDD-slug-format.md`
  - Format: `YYMMDD` (2-digit year, 2-digit month, 2-digit day)
  - Example: `250903-glp1-denials-what-to-do.md`
  - Use descriptive, SEO-friendly slugs with hyphens

## Frontmatter Format

Every blog post must include YAML frontmatter with these required fields:

```yaml
---
title: "Full Blog Post Title"
slug: "YYMMDD-descriptive-slug"
date: "YYYY-MM-DD"
author: "FHI Team"
description: "Brief subtitle or meta description for SEO"
tags: ["tag1", "tag2", "tag3", "relevant-keywords"]
readTime: "X min read"
---
```

### Frontmatter Guidelines

- **Title**: Use exact title provided, including subtitle if given
- **Slug**: Must match filename (without .md extension)
- **Date**: Use ISO format (YYYY-MM-DD) for current date
- **Author**: Default to "FHI Team"
- **Description**: Use provided subtitle or create brief SEO-friendly description
- **Tags**: Include relevant keywords like medical conditions, insurance terms, drug names
- **ReadTime**: Estimate based on content length (typically 4-6 min for standard posts)

## Content Structure

### Headers
- Use `##` for main sections (Markdown H2)
- Keep section titles descriptive and scannable
- Common sections: "The Landscape of [Topic]", "Changes to Coverage", "Here's What To Do"

### Linking Strategy
- **Inline Links**: Add hyperlinks to key terms and phrases throughout the content
- **Anchor Text**: Use descriptive words that relate to the linked content
- **Reference Style**: If links are added, all links must also appear in a References section at the end

### Text Preservation
- **Critical Rule**: Never alter the provided text content
- Only add markdown formatting (headers, links) and proper structure
- Maintain exact wording, punctuation, and paragraph breaks

## References Section (When Links Are Present)

If the blog post contains any inline links, include a `### References` section at the end with all linked sources:

```markdown
### References
- [Descriptive Link Title](https://full-url-here)
- [Another Source Title](https://another-url-here)
```

### Reference Guidelines
- Use descriptive titles that summarize the linked content
- List in order of appearance in the post
- Ensure every inline link has a corresponding reference entry
- Omit the References section entirely if the post contains no links

## Linking Best Practices

### Target Words/Phrases for Links
- Medical terms and conditions
- Insurance terminology ("prior authorization", "step therapy")
- Regulatory references ("American Diabetes Association", "Affordable Care Act")
- Statistical claims ("hundreds", "tightening")
- Process descriptions ("regulating", "exploring")

### Link Placement Strategy
- Distribute links naturally throughout the content
- Avoid clustering too many links in one paragraph
- Prioritize linking authoritative sources (medical institutions, government, established news outlets)

## Quality Checklist

Before finalizing a blog post, verify:

- [ ] Frontmatter includes all required fields
- [ ] Filename matches slug in frontmatter
- [ ] Date format is correct (YYYY-MM-DD)
- [ ] All inline links are working (if any)
- [ ] References section includes all linked sources (if links present)
- [ ] Content text remains unchanged from original
- [ ] Headers use proper markdown formatting
- [ ] Tags are relevant and include key terms
- [ ] Read time estimate is reasonable

## Example Post Structure

```markdown
---
title: "Example Blog Post Title"
slug: "250903-example-post"
date: "2025-09-03"
author: "FHI Team"
description: "Brief description for SEO"
tags: ["keyword1", "keyword2", "medical-term"]
readTime: "5 min read"
---

Opening paragraph with [linked term](https://source.com) and context.

## Main Section Header

Content with additional [linked phrases](https://another-source.com) and information.

## Another Section

More content with proper linking strategy.

## Here's What To Do

Actionable advice and conclusion.

### References
- [Source One Title](https://source.com)
- [Source Two Title](https://another-source.com)
```

## Technical Notes

- **File Format**: Markdown (.md)
- **Encoding**: UTF-8
- **Line Endings**: Unix/macOS style (LF)
- **Directory**: Always place in `/fighthealthinsurance/static/blog/`

This guide ensures consistency across all blog posts and maintains the established quality standards for the Fight Health Insurance project.