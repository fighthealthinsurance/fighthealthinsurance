/**
 * Fetches and displays live model health status with humorous copy
 */

interface ModelHealthDetail {
  name: string;
  ok: boolean;
  error?: string | null;
}

interface ModelHealthSnapshot {
  alive_models: number;
  last_checked: number;
  details: ModelHealthDetail[];
}

/**
 * Fetch the current model health status from the backend
 */
async function fetchModelHealth(): Promise<ModelHealthSnapshot | null> {
  try {
    const response = await fetch("/ziggy/rest/live_models_status", {
      method: "GET",
      headers: {
        Accept: "application/json",
      },
    });
    if (!response.ok) {
      console.warn("Model health check failed:", response.status);
      return null;
    }
    return await response.json();
  } catch (error) {
    console.error("Error fetching model health:", error);
    return null;
  }
}

/**
 * Generate humorous status copy based on model count
 */
function getStatusCopy(aliveCount: number): string {
  if (aliveCount === 0) {
    return "🥱 0 AI braincells awake — still warming up our magic wands";
  } else if (aliveCount === 1) {
    return "🧠 1 AI model ready to fight your insurance company";
  } else if (aliveCount <= 3) {
    return `🧠 ${aliveCount} AI models ready to fight your insurance company`;
  } else if (aliveCount <= 6) {
    return `🧠💪 ${aliveCount} AI models locked, loaded, and ready to fight`;
  } else {
    return `🧠💪🔥 ${aliveCount} AI models online — your insurance company is in trouble`;
  }
}

/**
 * Render the model status indicator in the specified container
 */
function renderModelStatus(
  container: HTMLElement,
  snapshot: ModelHealthSnapshot | null,
): void {
  if (!snapshot) {
    container.innerHTML = `
      <div class="model-status model-status-unknown">
        <span class="status-icon">⚙️</span>
        <span class="status-text">AI status check in progress...</span>
      </div>
    `;
    return;
  }

  const statusClass =
    snapshot.alive_models > 0 ? "model-status-ok" : "model-status-down";
  const statusCopy = getStatusCopy(snapshot.alive_models);

  container.innerHTML = `
    <div class="model-status ${statusClass}">
      <span class="status-text">${statusCopy}</span>
    </div>
  `;
}

/**
 * Initialize model status display and set up periodic refresh
 */
export async function initModelStatus(containerId: string = "model-status"): Promise<void> {
  const container = document.getElementById(containerId);
  if (!container) {
    console.warn(`Model status container #${containerId} not found`);
    return;
  }

  // Initial fetch
  const snapshot = await fetchModelHealth();
  renderModelStatus(container, snapshot);

  // Refresh every 5 minutes
  setInterval(async () => {
    const updatedSnapshot = await fetchModelHealth();
    renderModelStatus(container, updatedSnapshot);
  }, 5 * 60 * 1000);
}

// Auto-initialize on DOMContentLoaded if container exists
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    const container = document.getElementById("model-status");
    if (container) {
      initModelStatus("model-status");
    }
  });
}
