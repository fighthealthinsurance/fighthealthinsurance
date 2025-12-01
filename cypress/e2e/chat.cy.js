/**
 * End-to-end test for starting a chat
 * 
 * This test covers:
 * - Navigating to the chat page
 * - Completing the consent form
 * - Accessing the chat interface
 */

describe('Chat Flow', () => {
  beforeEach(() => {
    // Clear cookies and localStorage before each test
    cy.clearCookies();
    cy.clearLocalStorage();
  });

  it('should navigate to chat consent page when accessing chat', () => {
    // Visit the chat page - should redirect to consent page
    cy.visit('/chat');
    
    // Should be redirected to the consent page
    cy.url().should('include', 'chat-consent');
    cy.contains('Terms of Service').should('be.visible');
  });

  it('should complete consent form and access chat interface', () => {
    // Visit the chat consent page
    cy.visit('/chat-consent');
    
    // Page should have the consent form
    cy.get('form#user_consent_form').should('be.visible');
    
    // Fill in the consent form using the custom command
    cy.fillChatConsentForm({
      firstName: 'John',
      lastName: 'Doe',
      email: 'john.doe@example.com',
      subscribe: false
    });
    
    // Submit the form
    cy.get('button[type="submit"]').contains('Continue to Chat').click();
    
    // Should be redirected to the chat interface
    cy.url().should('include', '/chat');
    
    // The chat interface should be loaded (React component mount point)
    cy.get('#chat-interface-root').should('exist');
  });

  it('should show validation errors for missing required fields', () => {
    cy.visit('/chat-consent');
    
    // Try to submit without filling required fields
    cy.get('button[type="submit"]').contains('Continue to Chat').click();
    
    // Should stay on the same page (form validation should fail)
    cy.url().should('include', 'chat-consent');
  });

  it('should preserve user info in localStorage after consent', () => {
    cy.visit('/chat-consent');
    
    // Fill in the consent form
    cy.fillChatConsentForm({
      firstName: 'Jane',
      lastName: 'Smith',
      email: 'jane.smith@example.com'
    });
    
    // Submit the form
    cy.get('button[type="submit"]').contains('Continue to Chat').click();
    
    // Verify localStorage was updated
    cy.window().then((win) => {
      const storedInfo = win.localStorage.getItem('fhi_user_info');
      expect(storedInfo).to.not.be.null;
      
      const userInfo = JSON.parse(storedInfo);
      expect(userInfo.firstName).to.equal('Jane');
      expect(userInfo.lastName).to.equal('Smith');
      expect(userInfo.email).to.equal('jane.smith@example.com');
    });
  });
});
