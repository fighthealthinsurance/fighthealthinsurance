import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';

// Extend the Window interface to include our custom property
declare global {
    interface Window {
        blogSlugs?: string[];
    }
}

interface BlogPost {
  id: string;
  title: string;
  date: string;
  excerpt: string;
  slug: string;
  frontmatter?: Record<string, string>;
}

const BlogIndex: React.FC = () => {
  const [posts, setPosts] = useState<BlogPost[]>([]);
  const [loading, setLoading] = useState(true);
  const [failedSlugs, setFailedSlugs] = useState<string[]>([]);

  useEffect(() => {
    const loadPosts = async () => {
      try {
        // Get list of available blog posts from the data embedded in the HTML
        const knownSlugs = window.blogSlugs || [];
        const currentFailedSlugs: string[] = [];

        // Fetch all posts in parallel
        const postPromises = knownSlugs.map(async (slug) => {
          try {
            const response = await fetch(`/static/blog/${slug}.md`);
            if (!response.ok) {
                console.warn(`Failed to load post ${slug}: HTTP ${response.status}`);
                currentFailedSlugs.push(slug);
                return null;
            }
            const mdContent = await response.text();

            // Parse frontmatter
            let frontmatter: Record<string, string> = {};
            if (mdContent.startsWith('---\n')) {
              const frontmatterEnd = mdContent.indexOf('\n---\n', 4);
              if (frontmatterEnd !== -1) {
                const frontmatterText = mdContent.slice(4, frontmatterEnd).trim();
                // Simple YAML parsing
                frontmatterText.split('\n').forEach(line => {
                  const trimmedLine = line.trim();
                  if (!trimmedLine || trimmedLine.startsWith('#')) return;
                  
                  const colonIndex = trimmedLine.indexOf(':');
                  if (colonIndex > 0 && colonIndex < trimmedLine.length - 1) {
                    const key = trimmedLine.slice(0, colonIndex).trim();
                    let value = trimmedLine.slice(colonIndex + 1).trim().replace(/^(['"])(.*)\1$/, '$2');
                    frontmatter[key] = value;
                  }
                });
              }
            }

            // Extract excerpt from description or first paragraph
            let excerpt = frontmatter.description || '';
            if (!excerpt) {
              const content = mdContent.slice(mdContent.indexOf('\n---\n', 4) + 5).trim();
              const firstParagraph = content.split('\n\n')[0];
              excerpt = firstParagraph.replace(/[#*`]/g, '').substring(0, 150) + '...';
            }

            return {
              id: slug,
              title: frontmatter.title || slug.replace(/-/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
              date: frontmatter.date || '',
              excerpt,
              slug,
              frontmatter
            } as BlogPost;
          } catch (err) {
            console.warn(`Failed to load post ${slug}:`, err);
            currentFailedSlugs.push(slug);
            return null;
          }
        });

        const posts = (await Promise.all(postPromises)).filter(Boolean) as BlogPost[];

        // Sort posts by date string (newest first) to avoid timezone issues
        posts.sort((a, b) => b.date.localeCompare(a.date));

        setPosts(posts);
        setFailedSlugs(currentFailedSlugs);
      } catch (err) {
        console.error('Error loading posts:', err);
      } finally {
        setLoading(false);
      }
    };

    loadPosts();
  }, []);

  if (loading) {
    return <div className="fhi-section"><div className="fhi-column fhi-column-centred">Loading...</div></div>;
  }

  return (
    <div className="fhi-page-wide">
      {/* The same title block every content page opens with; the markup and
          classes mirror templates/partials/page_title.html, which a React
          root cannot include. */}
      <header className="fhi-page-title">
        <h1>Blog</h1>
        <p className="fhi-page-lede">
          Insights, tips, and strategies for fighting health insurance denials.
        </p>
      </header>

      <div className="fhi-stack fhi-stack-loose">
        {failedSlugs.length > 0 && (
          <div className="fhi-notice fhi-notice-warning" role="alert">
            <strong>Warning:</strong> Some blog posts could not be loaded: {failedSlugs.join(', ')}. This might be a deployment issue.
          </div>
        )}

        <div className="fhi-cards fhi-cards-roomy">
          {posts.map(post => (
            <div key={post.id} className="fhi-card">
              <h5 style={{color: 'var(--fhi-green-ink)'}}>{post.title}</h5>
              <p className="fhi-note">
                {post.date && (() => {
                  // Only format if date matches YYYY-MM-DD
                  const match = post.date.match(/^\d{4}-\d{2}-\d{2}$/);
                  if (match) {
                    const [year, month, day] = post.date.split('-');
                    const dateObj = new Date(Number(year), Number(month) - 1, Number(day));
                    if (!isNaN(dateObj.getTime())) {
                      return dateObj.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
                    }
                  }
                  // Fallback: show raw date
                  return post.date;
                })()}
              </p>
              <p>{post.excerpt}</p>
              <a href={`/blog/${post.slug}/`} className="fhi-button fhi-button-secondary fhi-card-action">
                Read More
              </a>
            </div>
          ))}
        </div>

        <div className="fhi-stack fhi-stack-centred">
          <p className="fhi-hint">
            More posts coming soon! Have a suggestion for a topic?{' '}
            <a href="/contact/" className="link">Let us know</a>.
          </p>
        </div>
      </div>
    </div>
  );
};

// Initialize the component when the DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  const container = document.getElementById('blog-root');
  if (container) {
    const root = createRoot(container);
    root.render(<BlogIndex />);
  }
});

export default BlogIndex;
