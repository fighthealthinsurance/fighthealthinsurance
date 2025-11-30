/**
 * End-to-end test for blog navigation
 * 
 * This test covers:
 * - Navigating to the blog page
 * - Verifying blog posts are displayed
 * - Clicking on the first article and viewing it
 */

describe('Blog Navigation', () => {
  beforeEach(() => {
    // Clear cookies before each test
    cy.clearCookies();
  });

  it('should load the blog page', () => {
    cy.visit('/blog/');
    
    // Check page title
    cy.title().should('include', 'Blog');
    
    // The blog root element should exist (React mount point)
    cy.get('#blog-root').should('exist');
  });

  it('should display blog posts', () => {
    cy.visit('/blog/');
    
    // Wait for the React component to render blog posts
    // Blog posts should be rendered as clickable elements/links
    cy.get('#blog-root').should('not.be.empty');
    
    // Wait for content to load (React component needs time to mount and fetch)
    cy.wait(1000);
    
    // There should be at least one blog post link or article element
    cy.get('#blog-root').find('a, article, [data-testid="blog-post"]').should('have.length.at.least', 1);
  });

  it('should navigate to a blog post when clicking on it', () => {
    cy.visit('/blog/');
    
    // Wait for blog posts to load
    cy.get('#blog-root').should('not.be.empty');
    cy.wait(1000);
    
    // Click on the first blog post link
    cy.get('#blog-root').find('a').first().click();
    
    // Should navigate to a blog post page
    cy.url().should('include', '/blog/');
    cy.url().should('not.eq', Cypress.config().baseUrl + '/blog/');
  });

  it('should have blog link accessible from the main site', () => {
    cy.visit('/');
    
    // Look for a link to the blog in the navigation or page
    cy.get('a[href*="blog"]').should('exist');
  });

  it('should display blog post content', () => {
    cy.visit('/blog/');
    
    // Wait for blog posts to load
    cy.get('#blog-root').should('not.be.empty');
    cy.wait(1000);
    
    // Click on the first blog post
    cy.get('#blog-root').find('a').first().click();
    
    // Wait for the blog post to load
    cy.wait(500);
    
    // The page should have some content - check for common blog elements
    cy.get('body').should('contain.text', 'health').or('contain.text', 'insurance').or('contain.text', 'appeal');
  });

  it('should be able to navigate back from a blog post', () => {
    cy.visit('/blog/');
    
    // Wait for blog posts to load
    cy.get('#blog-root').should('not.be.empty');
    cy.wait(1000);
    
    // Click on the first blog post
    cy.get('#blog-root').find('a').first().click();
    
    // Navigate back
    cy.go('back');
    
    // Should be back on the blog page
    cy.url().should('include', '/blog');
    cy.get('#blog-root').should('exist');
  });
});
