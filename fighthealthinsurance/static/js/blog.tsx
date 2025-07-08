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

  useEffect(() => {
    const loadPosts = async () => {
      try {
        // Get list of available blog posts from the data embedded in the HTML
        const knownSlugs = window.blogSlugs || [];

        // Fetch all posts in parallel
        const postPromises = knownSlugs.map(async (slug) => {
          try {
            const response = await fetch(`/static/blog/${slug}.mdx`);
            if (!response.ok) return null;
            const mdxContent = await response.text();

            // Parse frontmatter
            let frontmatter: Record<string, string> = {};
            if (mdxContent.startsWith('---')) {
              const frontmatterEnd = mdxContent.indexOf('\n---', 3);
              if (frontmatterEnd !== -1) {
                const frontmatterText = mdxContent.slice(3, frontmatterEnd).trim();
                // Simple YAML parsing
                frontmatterText.split('\n').forEach(line => {
                  const colonIndex = line.indexOf(':');
                  if (colonIndex > 0) {
                    const key = line.slice(0, colonIndex).trim();
                    let value = line.slice(colonIndex + 1).trim().replace(/^['"]|['"]$/g, '');
                    frontmatter[key] = value;
                  }
                });
              }
            }

            // Extract excerpt from description or first paragraph
            let excerpt = frontmatter.description || '';
            if (!excerpt) {
              const content = mdxContent.slice(mdxContent.indexOf('\n---', 3) + 4).trim();
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
            return null;
          }
        });

        const posts = (await Promise.all(postPromises)).filter(Boolean) as BlogPost[];

        // Sort posts by date string (newest first) to avoid timezone issues
        posts.sort((a, b) => b.date.localeCompare(a.date));

        setPosts(posts);
      } catch (err) {
        console.error('Error loading posts:', err);
      } finally {
        setLoading(false);
      }
    };

    loadPosts();
  }, []);

  if (loading) {
    return <div className="container mt-5"><div className="text-center">Loading...</div></div>;
  }

  return (
    <div className="container mt-5">
      <h2 style={{ marginTop: '10vh' }}>Fight Health Insurance Blog</h2>
      <p className="lead mb-4">
        Insights, tips, and strategies for fighting health insurance denials.
      </p>
      
      <div className="row">
        {posts.map(post => (
          <div key={post.id} className="col-md-6 mb-4">
            <div className="card h-100">
              <div className="card-body">
                <h5 className="card-title" style={{color: '#a5c422'}}>{post.title}</h5>
                <p className="card-text text-muted small mb-2">
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
                <p className="card-text">{post.excerpt}</p>
                <a href={`/blog/${post.slug}/`} className="btn" style={{backgroundColor: '#a5c422', color: 'white', border: 'none'}}>
                  Read More
                </a>
              </div>
            </div>
          </div>
        ))}
      </div>
      
      <div className="text-center mt-5">
        <p className="text-muted">
          More posts coming soon! Have a suggestion for a topic? 
          <a href="/contact/" className="link"> Let us know</a>.
        </p>
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
