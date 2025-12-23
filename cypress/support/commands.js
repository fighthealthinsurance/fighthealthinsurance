// ***********************************************
// This example commands.js shows you how to
// create various custom commands and overwrite
// existing commands.
//
// For more comprehensive examples of custom
// commands please read more here:
// https://on.cypress.io/custom-commands
// ***********************************************

// Custom command to fill in the chat consent form
Cypress.Commands.add('fillChatConsentForm', (options = {}) => {
  const {
    firstName = 'Test',
    lastName = 'User',
    email = 'test@example.com',
    subscribe = false
  } = options;

  cy.get('input#store_fname').type(firstName);
  cy.get('input#store_lname').type(lastName);
  cy.get('input#email').type(email);
  
  // Accept terms of service (required)
  cy.get('input#tos').check();
  
  // Accept privacy policy (required)
  cy.get('input#privacy').check();
  
  if (subscribe) {
    cy.get('input#subscribe').check();
  }
});

// Custom command to fill in the denial form
Cypress.Commands.add('fillDenialForm', (options = {}) => {
  const {
    firstName = 'Test',
    lastName = 'User',
    email = 'test@example.com',
    denialText = 'Your claim for medication has been denied as not medically necessary.'
  } = options;

  cy.get('input#store_fname').type(firstName);
  cy.get('input#store_lname').type(lastName);
  cy.get('input#email').type(email);
  cy.get('textarea#denial_text').type(denialText);
  
  // Check required consent boxes
  cy.get('input#pii').check();
  cy.get('input#privacy').check();
  cy.get('input#tos').check();
});
