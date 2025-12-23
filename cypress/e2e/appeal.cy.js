/**
 * End-to-end test for generating an appeal
 * 
 * This test covers:
 * - Navigating to the appeal page (scan page)
 * - Filling in the denial form
 * - Submitting and proceeding through the appeal generation flow
 */

describe('Appeal Generation Flow', () => {
  beforeEach(() => {
    // Clear cookies and localStorage before each test
    cy.clearCookies();
    cy.clearLocalStorage();
  });

  it('should navigate to the scan/upload denial page from home', () => {
    cy.visit('/');
    
    // Check the page title
    cy.title().should('include', 'Fight Your Health Insurance Denial');
    
    // Click on the scan link to start the appeal process
    cy.get('a#scanlink').click();
    
    // Should be on the scan/upload denial page
    cy.title().should('include', 'Upload your Health Insurance Denial');
  });

  it('should show validation error when submitting with missing required fields', () => {
    cy.visit('/scan');
    
    // Fill in only first name (missing required fields)
    cy.get('input#store_fname').type('Test User');
    
    // Click submit
    cy.get('button#submit').click();
    
    // Should show PII error (since we didn't check the PII checkbox)
    cy.get('div#pii_error').should('be.visible');
    
    // Should stay on the same page
    cy.title().should('include', 'Upload your Health Insurance Denial');
  });

  it('should proceed past the denial form with all required fields', () => {
    cy.visit('/scan');
    
    // Fill in the form using the custom command
    cy.fillDenialForm({
      firstName: 'Test',
      lastName: 'User',
      email: 'test@example.com',
      denialText: `Dear Test User;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Test-Insurance-Corp`
    });
    
    // Submit the form
    cy.get('button#submit').click();
    
    // Should proceed to the health history page
    cy.title().should('include', 'Optional: Health History');
  });

  it('should navigate through the full appeal generation flow', () => {
    cy.visit('/scan');
    
    // Step 1: Fill in the denial form
    cy.fillDenialForm({
      firstName: 'Test',
      lastName: 'User',
      email: 'test@example.com',
      denialText: `Dear Test User;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Test-Insurance-Corp`
    });
    
    // Submit the denial form
    cy.get('button#submit').click();
    
    // Step 2: Health History page - verify both title and URL change
    cy.title().should('include', 'Optional: Health History');
    cy.url().should('include', 'combined_collected_view');
    cy.get('button#next').should('be.visible').click();
    
    // Step 3: Plan Documents page - verify both title and URL
    cy.title().should('include', 'Optional: Add Plan Documents');
    cy.url().should('include', 'plan_documents');
    cy.get('button#next').should('be.visible').click();
    
    // Step 4: Categorize denial page - verify final page
    cy.title().should('include', 'Categorize Your Denial');
    cy.url().should('include', 'categorize');
  });

  it('should load the denial scan page directly', () => {
    cy.visit('/scan');
    
    // Check page title
    cy.title().should('include', 'Upload your Health Insurance Denial');
    
    // Check that the form exists
    cy.get('form#fuck_health_insurance_form').should('exist');
    
    // Check that required fields are present
    cy.get('input#store_fname').should('exist');
    cy.get('input#store_lname').should('exist');
    cy.get('input#email').should('exist');
    cy.get('textarea#denial_text').should('exist');
  });

  it('should have proper consent checkboxes', () => {
    cy.visit('/scan');
    
    // Check that all consent checkboxes exist
    cy.get('input#pii').should('exist');
    cy.get('input#privacy').should('exist');
    cy.get('input#tos').should('exist');
    
    // Check the subscribe checkbox is checked by default
    cy.get('input#subscribe').should('be.checked');
  });
});
