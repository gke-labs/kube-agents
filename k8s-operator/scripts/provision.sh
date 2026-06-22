#!/usr/bin/env bash
# ==============================================================================
# 🤖 Master GKE Standard & Cloud-Agnostic Operator E2E Provisioner
# ==============================================================================
# Orchestrates GCP/GKE bootstrapping, operator and agent container builds,
# manual GSA/PubSub setup, IAM configuration, and CR application.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/common.sh" "$@"

DRY_RUN_ARG=""
if [ "$DRY_RUN" -eq 1 ]; then
  DRY_RUN_ARG="--dry-run"
fi

echo -e "${C_MAGENTA}${C_BOLD}🚀 Starting GKE Platform Agent provisioning pipeline...${C_RESET}"

"${SCRIPT_DIR}/provision_01_gcp_cluster.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_02_gcp_gke_operator.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_03_gcp_iam.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_04_gcp_k8s_secrets.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_05_gcp_gchat.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_06_deploy_platform_agent.sh" $DRY_RUN_ARG

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Infrastructure & Cloud Resources Provisioned Successfully!  <<<${C_RESET}"

# Load state/variables via common helper
load_state


echo -e "${C_YELLOW}${C_BOLD}======================= START COPY&PASTE =======================${C_RESET}"
echo -e "${C_YELLOW}Your Kubernetes Operator and Custom Resources are ready!${C_RESET}"
echo -e "Next steps to run the operator and interact with your bot:\n"

echo -e "[ ] 1. Configure GChat bot connection in GCP Console:"
echo -e "       ${C_WHITE}https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${PROJECT_ID}${C_RESET}"
echo -e "       - Name: ${C_GREEN}GKE Platform Agent Bot${C_RESET}"
echo -e "       - Avatar: ${C_GREEN}https://platform-agent.nousresearch.com/docs/img/logo.png${C_RESET}"
echo -e "       - Connection Settings: Select ${C_BOLD}Cloud Pub/Sub${C_RESET}"
echo -e "       - Pub/Sub Topic Name: ${C_GREEN}projects/${PROJECT_ID}/topics/${CHAT_TOPIC_NAME}${C_RESET}"
echo -e "       - Under Visibility, check: ${C_GREEN}Only specific people (add your email/emails: ${ALLOWED_USERS:-your-email})${C_RESET}"

echo -e ""
echo -e "[ ] 2. Run the new Operator manager locally or deploy it:"
echo -e "       To run locally: ${C_WHITE}ENABLE_WEBHOOKS=false make run${C_RESET} (from k8s-operator directory)"
echo -e "       To deploy to cluster: ${C_WHITE}make deploy IMG=<your-docker-registry>/kube-agents-operator:latest${C_RESET}"

echo -e ""
echo -e "[ ] 3. Monitor Gateway pod rollout progress:"
echo -e "       ${C_WHITE}kubectl get pods -n ${NAMESPACE:-kubeagents-system}${C_RESET}"

echo -e ""
echo -e "[ ] 4. Send a DM to the Bot on Google Chat:"
echo -e "       Type: ${C_WHITE}\"Hi Hermes\"${C_RESET}"

echo -e ""
echo -e "[ ] 5. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container:"
echo -e "       ${C_CYAN}(Only required for first-time bot deployments. If the bot responds instantly, skip this!)${C_RESET}"
echo -e "       ${C_WHITE}kubectl exec -it deploy/platform-agent-gateway -n ${NAMESPACE:-kubeagents-system} -- hermes pairing approve google_chat <PAIRING_CODE>${C_RESET}"
if [ "$MODEL_PROVIDER" = "chatgpt" ]; then
  echo -e ""
  echo -e "[ ] 6. ${C_YELLOW}Complete ChatGPT OAuth Device Flow Authentication:${C_RESET}"
  echo -e "       Because you selected 'chatgpt' as the model provider, LiteLLM must be authenticated"
  echo -e "       via OpenAI's OAuth Device Flow. Please follow these steps to authenticate:"
  echo -e "       - View the LiteLLM gateway logs to retrieve the 8-digit user code:"
  echo -e "         ${C_WHITE}kubectl logs -n ${NAMESPACE:-kubeagents-system} deployment/litellm -f${C_RESET}"
  echo -e "       - Open your browser and navigate to: ${C_WHITE}https://auth.openai.com/codex/device${C_RESET}"
  echo -e "       - Enter the code displayed in the LiteLLM logs and log in to authorize the device."
  echo -e "       - Once authorized, the LiteLLM gateway will automatically pair with your ChatGPT subscription."
fi

echo -e "======================== END COPY&PASTE ========================\n"
