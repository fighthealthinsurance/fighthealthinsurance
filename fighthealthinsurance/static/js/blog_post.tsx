import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { marked, Renderer } from 'marked';
import DOMPurify from 'dompurify';

// Add custom renderer to generate heading ids for quick navigation links
const renderer = new Renderer();
// Override heading to include slugger for generating IDs and support explicit ID syntax
renderer.heading = (text: string, level: number, raw: string) => {
  // Check for explicit ID in markdown like {#custom-id}
  const explicitIdMatch = raw.match(/(.+?)\s*\{#([\w-]+)\}$/);
  let slug: string;
  let headingText: string = text;
  if (explicitIdMatch) {
    headingText = explicitIdMatch[1].trim();
    slug = explicitIdMatch[2];
  } else {
    slug = raw
      .toLowerCase()
      .replace(/[^\x00-\x7F\w\s-]/g, '') // remove non-ascii
      .trim()
      .replace(/[\s]+/g, '-') // spaces to dashes
      .replace(/-+/g, '-');
  }
  return `<h${level} id="${slug}">${headingText}</h${level}>`;
};

// Configure marked to properly handle links and headings
marked.setOptions({
  gfm: true,
  breaks: true,
  renderer
});

interface BlogPostProps {
  slug: string;
  type?: 'blog' | 'faq';
}

const BlogPost: React.FC<BlogPostProps> = ({ slug, type = 'blog' }) => {
  const [content, setContent] = useState<string>('');
  const [metadata, setMetadata] = useState<Record<string, string>>({});
  const [authorHtml, setAuthorHtml] = useState<string>('');
  const [leadingContent, setLeadingContent] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');
  
  // Use prop-based detection for FAQ vs blog
  const isFAQ = type === 'faq';

  useEffect(() => {
    // Add custom styles for MD content
    const styleId = 'md-content-styles';
    if (!document.getElementById(styleId)) {
      const style = document.createElement('style');
      style.id = styleId;
      style.textContent = `
        .md-content h1 {
          font-size: 2rem;
          font-weight: 600;
          margin-top: 2rem;
          margin-bottom: 1rem;
          color: #333;
        }
        .md-content h2 {
          font-size: 1.5rem;
          font-weight: 600;
          margin-top: 1.75rem;
          margin-bottom: 0.75rem;
          color: #333;
        }
        .md-content h3 {
          font-size: 1.25rem;
          font-weight: 600;
          margin-top: 1.5rem;
          margin-bottom: 0.5rem;
          color: #333;
        }
        .md-content h4 {
          font-size: 1.125rem;
          font-weight: 600;
          margin-top: 1.25rem;
          margin-bottom: 0.5rem;
          color: #333;
        }
        .md-content h5 {
          font-size: 1rem;
          font-weight: 600;
          margin-top: 1rem;
          margin-bottom: 0.5rem;
          color: #333;
        }
        .md-content h6 {
          font-size: 0.875rem;
          font-weight: 600;
          margin-top: 1rem;
          margin-bottom: 0.5rem;
          color: #333;
        }
        .md-content p {
          margin-bottom: 1rem;
        }
        .md-content ul, .md-content ol {
          margin-bottom: 1rem;
        }
        .md-content li {
          margin-bottom: 0.25rem;
        }
        .md-content a {
          color: #a5c422;
          text-decoration: underline;
        }
        .md-content a:hover {
          color: #8eb31d;
          text-decoration: underline;
        }
        .md-content img {
          max-width: 100%;
          height: auto;
          border-radius: 4px;
          box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
          margin: 1rem 0;
        }
        .md-content a[href^="#"]:before {
          content: "";
          margin-right: 0.25rem;
        }
        .md-content a[href^="#"]:hover {
          text-decoration: none;
          color: #8eb31d;
        }
        .author-line a {
          color: #a5c422;
          text-decoration: underline;
        }
        .author-line a:hover {
          color: #8eb31d;
          text-decoration: underline;
        }
      `;
      document.head.appendChild(style);
    }

    // Load static Markdown content for blog/FAQ posts
    const loadPost = async () => {
      try {
        // Use the already computed isFAQ boolean
        const baseUrl = isFAQ ? '/static/faq' : '/static/blog';
        // This is a simplified version - in reality you'd use MD loader
        const response = await fetch(`${baseUrl}/${slug}.md`);
        if (!response.ok) {
          throw new Error('Post not found');
        }
        const mdContent = await response.text();
        
        // Manual frontmatter parsing with validation
        const fm: Record<string, string> = {};
        let contentBody = mdContent;
        if (mdContent.startsWith('---\n')) {
          const endIndex = mdContent.indexOf('\n---\n', 4);
          if (endIndex !== -1) {
            const fmText = mdContent.slice(4, endIndex).trim();
            contentBody = mdContent.slice(endIndex + 5);
            fmText.split('\n').forEach(line => {
              const trimmedLine = line.trim();
              if (!trimmedLine || trimmedLine.startsWith('#')) return;

              const colonIndex = trimmedLine.indexOf(':');
              if (colonIndex > 0 && colonIndex < trimmedLine.length - 1) {
                const key = trimmedLine.slice(0, colonIndex).trim();
                let value = trimmedLine.slice(colonIndex + 1).trim().replace(/^(['"])(.*)\1$/, '$2');

                // Validate key contains only valid characters
                if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(key)) {
                  const msg = `Invalid frontmatter key: ${key}`;
                  if (process.env.NODE_ENV === 'development') throw new Error(msg);
                  console.warn(msg);
                  return;
                }

                fm[key] = value;
              } else {
                const msg = `Malformed frontmatter line: ${line}`;
                if (process.env.NODE_ENV === 'development') throw new Error(msg);
                console.warn(msg);
              }
            });
          }
        }
        setMetadata(fm);
        if (fm.author) {
            const rawAuthorHtml = await marked.parseInline(fm.author);
            const safeAuthorHtml = DOMPurify.sanitize(rawAuthorHtml);
            setAuthorHtml(safeAuthorHtml);
        }

        // Separate leading HTML from main markdown content
        let mainContent = contentBody;
        // Regex to find a block of HTML at the start of the string.
        // It looks for a string that starts with a tag, and is followed by a separator.
        const separator = '\n---\n';
        const contentParts = contentBody.split(separator);
        
        if (contentParts.length > 1) {
            const potentialLeadingContent = contentParts[0].trim();
            // A simple check to see if it's likely HTML
            if (potentialLeadingContent.startsWith('<') && potentialLeadingContent.endsWith('>')) {
                setLeadingContent(DOMPurify.sanitize(potentialLeadingContent));
                mainContent = contentParts.slice(1).join(separator);
            }
        }

        // Remove the H1 title from content body
        let processedContent = mainContent.replace(/^# .*$/m, '').trim();
        
        // Handle JSX-style image tags before markdown processing
        processedContent = processedContent.replace(
          /<img\s+([^>]*)\s*\/>/g,
          (match: string, attributes: string) => {
            // Convert JSX-style attributes to regular HTML
            let htmlAttributes = attributes
              // Handle style={{...}} JSX objects
              .replace(/style=\{\{([^}]+)\}\}/g, (_styleMatch: string, styleContent: string) => {
                // Convert JavaScript object notation to CSS
                const cssStyle = styleContent
                  .split(',')
                  .map((prop: string) => prop.trim())
                  .map((prop: string) => {
                    const [key, value] = prop.split(':').map((p: string) => p.trim());
                    // Convert camelCase to kebab-case
                    const cssKey = key.replace(/([A-Z])/g, '-$1').toLowerCase();
                    // Remove quotes from value
                    const cssValue = value.replace(/^['"]|['"]$/g, '');
                    return `${cssKey}: ${cssValue}`;
                  })
                  .join('; ');
                return `style="${cssStyle}"`;
              })
              // Fix image paths from /static/img/ to /static/images/
              .replace(/src="\/static\/img\//g, 'src="/static/images/');
            
            return `<img ${htmlAttributes} />`;
          }
        );
        
        // Convert markdown to HTML and sanitize
        const rawHtml = await marked.parse(processedContent);
        const safeHtml = DOMPurify.sanitize(rawHtml);
        
        setContent(safeHtml);
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Unknown error';
        setError(`Failed to load content: ${errorMessage}`);
        console.error('Error loading content:', {
          slug,
          error: err,
          timestamp: new Date().toISOString()
        });
      } finally {
        setLoading(false);
      }
    };

    loadPost();
  }, [slug]);

  if (loading) {
    return (
      <div className="container mt-5">
        <div className="text-center">
          <div className="spinner-border text-success" role="status">
            <span className="visually-hidden">Loading...</span>
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    const backUrl = isFAQ ? '/faq/' : '/blog/';
    const backText = isFAQ ? 'Back to FAQ' : 'Back to Blog';
    const contentType = isFAQ ? 'FAQ content' : 'blog post';
    
    return (
      <div className="container mt-5">
        <div className="alert alert-danger">
          <h4>Content Not Found</h4>
          <p>The {contentType} you're looking for doesn't exist.</p>
          <a href={backUrl} className="btn btn-success">{backText}</a>
        </div>
      </div>
    );
  }

  return (
    <div className="container mt-5">
      <div style={{ marginTop: '10vh', maxWidth: '800px', margin: '10vh auto 0 auto', padding: '0 20px' }}>
        <nav aria-label="breadcrumb" className="mb-4">
          <ol className="breadcrumb">
            <li className="breadcrumb-item">
              <a href={isFAQ ? '/faq/' : '/blog/'} style={{color: '#a5c422'}}>
                {isFAQ ? 'FAQ' : 'Blog'}
              </a>
            </li>
            <li className="breadcrumb-item active" aria-current="page">
              {metadata.title || slug.replace(/-/g, ' ')}
            </li>
          </ol>
        </nav>
        
        <div>
          <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '1rem' }}>
            {metadata.title || slug.replace(/-/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}
          </h1>
          
          <div style={{ color: '#6c757d', marginBottom: '2rem' }} className="author-line">
             {authorHtml && <div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(`By ${authorHtml}`) }} />}
            {metadata.date && (
              <div>
                {(() => {
                  // Only format if date matches YYYY-MM-DD
                  const match = metadata.date.match(/^\d{4}-\d{2}-\d{2}$/);
                  if (match) {
                    const [year, month, day] = metadata.date.split('-');
                    const dateObj = new Date(Number(year), Number(month) - 1, Number(day));
                    if (!isNaN(dateObj.getTime())) {
                      return dateObj.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
                    }
                  }
                  // Fallback: show raw date
                  return metadata.date;
                })()}
              </div>
            )}
          </div>
          
          {leadingContent && <div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(leadingContent) }} />}

          {/*
            Double-sanitize HTML at render time for defense-in-depth.
            Even though content is sanitized before setContent, we sanitize again here
            to protect against any future changes or missed edge cases in the pipeline.
            This is a best practice for robust XSS protection.
          */}
          <div 
            dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(content) }} 
            className="md-content"
            style={{
              fontSize: '1rem',
              lineHeight: '1.6'
            }}
          />
          
          <hr style={{ margin: '3rem 0' }} />
          
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '1rem' }}>
            <div className="card">
              <div className="card-body">
                <h5 className="card-title" style={{color: '#a5c422'}}>Need Help?</h5>
                <p className="card-text">
                  Get personalized assistance with your health insurance appeal.
                </p>
                <a href="/" className="btn" style={{backgroundColor: '#a5c422', color: 'white', border: 'none'}}>Start Appeal Generator</a>
              </div>
            </div>
            <div className="card">
              <div className="card-body">
                <h5 className="card-title" style={{color: '#a5c422'}}>More Resources</h5>
                <p className="card-text">
                  Explore additional tools and information to fight denials.
                </p>
                <a href="/other-resources" className="btn" style={{backgroundColor: 'transparent', color: '#a5c422', border: '1px solid #a5c422'}}>View Resources</a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

// Initialize the component when the DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  const blogContainer = document.getElementById('blog-post-root');
  const faqContainer = document.getElementById('faq-post-root');
  const container = blogContainer || faqContainer;
  
  if (container) {
    // Determine type based on container
    const type = faqContainer ? 'faq' : 'blog';
    
    // Get slug from URL or data attribute
    const slug = container.dataset.slug || window.location.pathname.split('/').pop();
    const root = createRoot(container);
    root.render(<BlogPost slug={slug || ''} type={type} />);
  }
});

export default BlogPost;
