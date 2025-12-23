/**
 * User info storage module - shared between consent forms and chat interface.
 * Handles localStorage persistence of user information with privacy scrubbing support.
 */

// Storage key for user info
const USER_INFO_KEY = "fhi_user_info";
// Storage key for external models preference
const EXTERNAL_MODELS_KEY = "fhi_use_external_models";

// Interface for user information
export interface UserInfo {
  firstName: string;
  lastName: string;
  email: string;
  address: string;
  city: string;
  state: string;
  zipCode: string;
  acceptedTerms: boolean;
}

/**
 * Save user info to localStorage
 */
export function saveUserInfo(userInfo: UserInfo): void {
  try {
    localStorage.setItem(USER_INFO_KEY, JSON.stringify(userInfo));
  } catch (e) {
    console.error("Error saving user info to localStorage:", e);
  }
}

/**
 * Get user info from localStorage
 */
export function getUserInfo(): UserInfo | null {
  try {
    const stored = localStorage.getItem(USER_INFO_KEY);
    if (stored) {
      return JSON.parse(stored) as UserInfo;
    }
  } catch (e) {
    console.error("Error parsing stored user info:", e);
  }
  return null;
}

/**
 * Clear user info from localStorage
 */
export function clearUserInfo(): void {
  localStorage.removeItem(USER_INFO_KEY);
}

/**
 * Save external models preference to localStorage
 */
export function saveExternalModelsPreference(useExternalModels: boolean): void {
  try {
    localStorage.setItem(EXTERNAL_MODELS_KEY, useExternalModels.toString());
  } catch (e) {
    console.error("Error saving external models preference to localStorage:", e);
  }
}

/**
 * Get external models preference from localStorage
 */
export function getExternalModelsPreference(): boolean {
  try {
    const stored = localStorage.getItem(EXTERNAL_MODELS_KEY);
    return stored === "true";
  } catch (e) {
    console.error("Error getting external models preference from localStorage:", e);
  }
  return false;
}

/**
 * Helper function to escape special regex characters in a string
 */
function escapeRegExp(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Scrub personal info from a message, replacing with placeholders
 */
export function scrubPersonalInfo(message: string, userInfo: UserInfo | null): string {
  if (!userInfo || !message) return message;

  let scrubbedMessage = message;

  // Replace name with word boundaries
  if (userInfo.firstName) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.firstName)}\\b`, "gi"),
      "[FIRST_NAME]"
    );
  }

  if (userInfo.lastName) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.lastName)}\\b`, "gi"),
      "[LAST_NAME]"
    );
  }

  // Replace address
  if (userInfo.address) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(escapeRegExp(userInfo.address), "gi"),
      "[ADDRESS]"
    );
  }

  // Replace city
  if (userInfo.city) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.city)}\\b`, "gi"),
      "[CITY]"
    );
  }

  // Replace state
  if (userInfo.state) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.state)}\\b`, "gi"),
      "[STATE]"
    );
  }

  // Replace zip code
  if (userInfo.zipCode) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.zipCode)}\\b`, "gi"),
      "[ZIP_CODE]"
    );
  }

  // Replace email
  if (userInfo.email) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(escapeRegExp(userInfo.email), "gi"),
      "[EMAIL]"
    );
  }

  return scrubbedMessage;
}

/**
 * Restore personal info in a message, replacing placeholders with actual values
 */
export function restorePersonalInfo(message: string, userInfo: UserInfo | null): string {
  if (!userInfo || !message) return message;

  let restoredMessage = message;

  if (userInfo.firstName) {
    restoredMessage = restoredMessage.replace(/\[FIRST_NAME\]/g, userInfo.firstName);
  }

  if (userInfo.lastName) {
    restoredMessage = restoredMessage.replace(/\[LAST_NAME\]/g, userInfo.lastName);
  }

  if (userInfo.address) {
    restoredMessage = restoredMessage.replace(/\[ADDRESS\]/g, userInfo.address);
  }

  if (userInfo.city) {
    restoredMessage = restoredMessage.replace(/\[CITY\]/g, userInfo.city);
  }

  if (userInfo.state) {
    restoredMessage = restoredMessage.replace(/\[STATE\]/g, userInfo.state);
  }

  if (userInfo.zipCode) {
    restoredMessage = restoredMessage.replace(/\[ZIP_CODE\]/g, userInfo.zipCode);
  }

  if (userInfo.email) {
    restoredMessage = restoredMessage.replace(/\[EMAIL\]/g, userInfo.email);
  }

  return restoredMessage;
}

// Form field IDs that map to UserInfo properties
const FIELD_MAPPINGS: Record<string, keyof UserInfo> = {
  store_fname: "firstName",
  store_lname: "lastName",
  email: "email",
  store_street: "address",
  store_city: "city",
  store_state: "state",
  store_zip: "zipCode",
};

/**
 * Collect user info from form fields
 */
export function collectUserInfoFromForm(): UserInfo {
  const getFieldValue = (id: string): string => {
    const elem = document.getElementById(id) as HTMLInputElement | null;
    return elem?.value || "";
  };

  return {
    firstName: getFieldValue("store_fname"),
    lastName: getFieldValue("store_lname"),
    email: getFieldValue("email"),
    address: getFieldValue("store_street"),
    city: getFieldValue("store_city"),
    state: getFieldValue("store_state"),
    zipCode: getFieldValue("store_zip"),
    acceptedTerms: true,
  };
}

/**
 * Populate form fields from stored user info
 */
export function populateFormFromUserInfo(userInfo: UserInfo | null): void {
  if (!userInfo) return;

  const setFieldValue = (id: string, value: string | undefined) => {
    if (!value) return;
    const elem = document.getElementById(id) as HTMLInputElement | null;
    if (elem && !elem.value) {
      // Only set if field is empty (don't overwrite server-provided values)
      elem.value = value;
    }
  };

  setFieldValue("store_fname", userInfo.firstName);
  setFieldValue("store_lname", userInfo.lastName);
  setFieldValue("email", userInfo.email);
  setFieldValue("store_street", userInfo.address);
  setFieldValue("store_city", userInfo.city);
  setFieldValue("store_state", userInfo.state);
  setFieldValue("store_zip", userInfo.zipCode);
}

/**
 * Setup form persistence - call on DOMContentLoaded
 * Handles both saving on submit and restoring on load
 */
export function setupFormPersistence(formId: string): void {
  const form = document.getElementById(formId) as HTMLFormElement | null;
  if (!form) {
    console.error(`Form with id "${formId}" not found`);
    return;
  }

  // Restore form fields from localStorage
  const storedInfo = getUserInfo();
  if (storedInfo) {
    populateFormFromUserInfo(storedInfo);
  }

  // Restore external models preference
  const externalModelsCheckbox = document.getElementById("use_external_models") as HTMLInputElement | null;
  if (externalModelsCheckbox) {
    externalModelsCheckbox.checked = getExternalModelsPreference();
  }

  // Save to localStorage on form submit
  form.addEventListener("submit", () => {
    const userInfo = collectUserInfoFromForm();
    saveUserInfo(userInfo);

    // Save external models preference
    if (externalModelsCheckbox) {
      saveExternalModelsPreference(externalModelsCheckbox.checked);
    }
  });
}

// Auto-initialize when imported as a script (for non-bundled usage)
if (typeof window !== "undefined") {
  // Export to window for template script usage
  (window as any).userInfoStorage = {
    saveUserInfo,
    getUserInfo,
    clearUserInfo,
    saveExternalModelsPreference,
    getExternalModelsPreference,
    scrubPersonalInfo,
    restorePersonalInfo,
    collectUserInfoFromForm,
    populateFormFromUserInfo,
    setupFormPersistence,
  };
}
