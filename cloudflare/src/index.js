export function isAthensTenAM(scheduledTime) {
  const hour = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Athens",
    hour: "2-digit",
    hourCycle: "h23",
  }).format(new Date(scheduledTime));
  return hour === "10";
}

function getWorkflowDispatchUrl(repository) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository || "")) {
    throw new Error("GITHUB_REPOSITORY must use the owner/repository format");
  }
  return `https://api.github.com/repos/${repository}/actions/workflows/daily-prices.yml/dispatches`;
}

export default {
  async fetch() {
    return new Response("Not found", { status: 404 });
  },

  async scheduled(controller, env) {
    if (!isAthensTenAM(controller.scheduledTime)) {
      console.info("Skipping UTC cron that is outside 10:00 Europe/Athens");
      return;
    }
    const workflowDispatchUrl = getWorkflowDispatchUrl(env.GITHUB_REPOSITORY);
    if (!env.GITHUB_TOKEN) {
      throw new Error("Missing Cloudflare secret GITHUB_TOKEN");
    }

    const response = await fetch(workflowDispatchUrl, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "X-GitHub-Api-Version": "2026-03-10",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { scheduled: true },
      }),
    });

    if (!response.ok) {
      throw new Error(`GitHub workflow dispatch failed with HTTP ${response.status}`);
    }
    console.info("Dispatched the daily price collection workflow");
  },
};
