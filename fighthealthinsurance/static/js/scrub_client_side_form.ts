// Add text
export function addText(text: string): void {
  const input = document.getElementById("denial_text") as HTMLTextAreaElement;
  input.value += text;
}

// Error messages
function rehideHiddenMessage(name: string): void {
  const element = document.getElementById(name);
  if (element) {
    element.classList.remove("visible");
  }
}
function showHiddenMessage(name: string): void {
  const element = document.getElementById(name);
  if (element) {
    element.classList.add("visible");
  }
}
export function hideErrorMessages(event: Event): void {
  const form = document.getElementById(
    "fuck_health_insurance_form",
  ) as HTMLFormElement | null; // Added | null
  if (form == null) {
    return;
  }
  if (form.privacy.checked && form.personalonly.checked && form.tos.checked) {
    rehideHiddenMessage("agree_chk_error");
  }
  if (form.pii.checked) {
    rehideHiddenMessage("pii_error");
  }
  if (form.email.value.length > 1) {
    const emailLabel = document.getElementById("email-label");
    if (emailLabel) {
      emailLabel.style.color = "";
    }
    rehideHiddenMessage("email_error");
  }
  if (form.denial_text.value.length > 1) {
    const denialTextLabel = document.getElementById("denial_text_label");
    if (denialTextLabel) {
      denialTextLabel.style.color = "";
    }
    rehideHiddenMessage("need_denial");
  }
}
export function validateScrubForm(event: Event): void {
  const form = event.target as HTMLFormElement | null; // Added | null
  if (!form) {
    event.preventDefault(); // Prevent submission if form is not found
    return;
  }
  if (
    !form.privacy.checked ||
    !form.personalonly.checked ||
    !form.tos.checked
  ) {
    showHiddenMessage("agree_chk_error");
  } else {
    rehideHiddenMessage("agree_chk_error");
  }
  if (!form.pii.checked) {
    showHiddenMessage("pii_error");
  } else {
    rehideHiddenMessage("pii_error");
  }
  if (form.email.value.length < 1) {
    showHiddenMessage("email_error");
    const emailLabel = document.getElementById("email-label");
    if (emailLabel) {
      emailLabel.style.color = "red";
    }
  } else {
    const emailLabel = document.getElementById("email-label");
    if (emailLabel) {
      emailLabel.style.color = "";
    }
    rehideHiddenMessage("email_error");
  }
  if (form.denial_text.value.length < 1) {
    showHiddenMessage("need_denial");
    const denialTextLabel = document.getElementById("denial_text_label");
    if (denialTextLabel) {
      denialTextLabel.style.color = "red";
    }
  } else {
    const denialTextLabel = document.getElementById("denial_text_label");
    if (denialTextLabel) {
      denialTextLabel.style.color = "";
    }
    rehideHiddenMessage("need_denial");
  }

  if (form.pii.checked && form.privacy.checked && form.email.value.length > 0) {
    rehideHiddenMessage("agree_chk_error");
    rehideHiddenMessage("pii_error");
    rehideHiddenMessage("email_error");
    rehideHiddenMessage("need_denial");
    // YOLO
    return;
  } else {
    // Bad news no submit
    event.preventDefault();
  }
}

function storeInLocalStorage(): void {
  const ids = [
    "fname",
    "lname",
    "dob",
    "email_address",
    "subscriber_id",
    "group_id",
    "plan_name",
    "insurance_company",
    "claim_id",
    "date_of_service",
    "denial_reason",
    "denial_text",
    "notes",
  ];
  ids.forEach((id) => {
    const element = document.getElementById(
      id,
    ) as HTMLInputElement | HTMLTextAreaElement | null;
    if (element) {
      localStorage.setItem(id, element.value);
    }
  });
}

function retrieveFromLocalStorage(): void {
  const ids = [
    "fname",
    "lname",
    "dob",
    "email_address",
    "subscriber_id",
    "group_id",
    "plan_name",
    "insurance_company",
    "claim_id",
    "date_of_service",
    "denial_reason",
    "denial_text",
    "notes",
  ];
  ids.forEach((id) => {
    const element = document.getElementById(
      id,
    ) as HTMLInputElement | HTMLTextAreaElement | null;
    if (element) {
      element.value = localStorage.getItem(id) || "";
    }
  });
}

function toggleSection(name: string): void {
  const element = document.getElementById(name);
  if (element) {
    if (element.classList.contains("visible")) {
      element.classList.remove("visible");
    } else {
      element.classList.add("visible");
    }
  }
}

function validateAndStore(): boolean {
  let isValid = true;
  const emailElement = document.getElementById(
    "email_address",
  ) as HTMLInputElement | null;
  const denialTextElement = document.getElementById(
    "denial_text",
  ) as HTMLTextAreaElement | null;
  const emailLabel = document.getElementById("email-label");
  const denialTextLabel = document.getElementById("denial_text_label");

  if (emailElement && emailLabel) {
    if (emailElement.value === "" || !emailElement.value.includes("@")) {
      emailLabel.style.color = "red";
      isValid = false;
    } else {
      emailLabel.style.color = "";
    }
  } else {
    isValid = false; // Element not found, consider it invalid
  }

  if (denialTextElement && denialTextLabel) {
    if (denialTextElement.value === "") {
      denialTextLabel.style.color = "red";
      isValid = false;
    } else {
      denialTextLabel.style.color = "";
    }
  } else {
    isValid = false; // Element not found
  }

  if (isValid) {
    storeInLocalStorage();
    alert("Information Stored in Local Storage");
  } else {
    alert(
      "Please fill out all required fields correctly (Email and Denial Text).",
    );
  }
  return isValid;
}

document.addEventListener("DOMContentLoaded", (event) => {
  retrieveFromLocalStorage();
  const form = document.getElementById("scrubform") as HTMLFormElement | null;
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault(); // stop form from submitting
      validateAndStore();
    });
  }

  const storeButton = document.getElementById(
    "storeButton",
  ) as HTMLButtonElement | null;
  if (storeButton) {
    storeButton.addEventListener("click", () => {
      validateAndStore();
    });
  }

  const toggleButtons = document.querySelectorAll(".toggle-button");
  toggleButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const sectionName = button.getAttribute("data-section");
      if (sectionName) {
        toggleSection(sectionName);
      }
    });
  });
});

export {
  storeInLocalStorage,
  retrieveFromLocalStorage,
  toggleSection,
  validateAndStore,
};
