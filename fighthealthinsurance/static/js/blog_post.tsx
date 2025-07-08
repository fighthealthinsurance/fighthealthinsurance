import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { marked } from 'marked';
import DOMPurify from 'dompurify';

interface BlogPostProps {
  slug: string;
}

const BlogPost: React.FC<BlogPostProps> = ({ slug }) => {
  const [content, setContent] = useState<string>('');
  const [metadata, setMetadata] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    // For now, we'll load static content. In a full implementation,
    // this would dynamically import and render the MDX
    const loadPost = async () => {
      try {
        // This is a simplified version - in reality you'd use MDX loader
        const response = await fetch(`/static/blog/${slug}.mdx`);
        if (!response.ok) {
          throw new Error('Post not found');
        }
        const mdxContent = await response.text();
        
        // Manual frontmatter parsing to avoid Buffer in browser
        const fm: Record<string,string> = {};
        let contentBody = mdxContent;
        if (mdxContent.startsWith('---')) {
          const parts = mdxContent.split('---', 3);
          const fmText = parts[1].trim();
          contentBody = parts[2] || '';
          fmText.split('\n').forEach(line => {
            const idx = line.indexOf(':');
            if (idx > 0) {
              const key = line.slice(0, idx).trim();
              let val = line.slice(idx + 1).trim().replace(/^['"]|['"]$/g, '');
              fm[key] = val;
            }
          });
        }
        setMetadata(fm);
        // Remove the H1 title from content body
        const processedContent = contentBody.replace(/^# .*$/m, '').trim();
        // Convert markdown to HTML and sanitize
  const rawHtml = await marked.parse(processedContent);
        const safeHtml = DOMPurify.sanitize(rawHtml);
        setContent(safeHtml);
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Unknown error';
        setError(`Failed to load blog post: ${errorMessage}`);
        console.error('Error loading post:', {
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
    return (
      <div className="container mt-5">
        <div className="alert alert-danger">
          <h4>Post Not Found</h4>
          <p>The blog post you're looking for doesn't exist.</p>
          <a href="/blog/" className="btn btn-success">Back to Blog</a>
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
              <a href="/blog/" style={{color: '#a5c422'}}>Blog</a>
            </li>
            <li className="breadcrumb-item active" aria-current="page">
              {metadata.title || slug.replace(/-/g, ' ')}
            </li>
          </ol>
        </nav>
        
        <div>
          <h1 style={{ fontSize: '2.5rem', fontWeight: 'bold', marginBottom: '1rem' }}>
            {metadata.title || slug.replace(/-/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}
          </h1>
          
          <div style={{ color: '#6c757d', marginBottom: '2rem' }}>
            {metadata.author && <div>By {metadata.author}</div>}
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
          
          {/*
            Double-sanitize HTML at render time for defense-in-depth.
            Even though content is sanitized before setContent, we sanitize again here
            to protect against any future changes or missed edge cases in the pipeline.
            This is a best practice for robust XSS protection.
          */}
          <div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(content) }} />
          
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
  const container = document.getElementById('blog-post-root');
  if (container) {
    // Get slug from URL or data attribute
    const slug = container.dataset.slug || window.location.pathname.split('/').pop();
    const root = createRoot(container);
    root.render(<BlogPost slug={slug || ''} />);
  }
});

export default BlogPost;
