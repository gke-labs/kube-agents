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
"${SCRIPT_DIR}/provision_04_gcp_gchat.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_05_slack.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_06_gcp_k8s_secrets.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_07_deploy_platform_agent.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_08_deploy_litellm.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/provision_09_deploy_github_minter.sh" $DRY_RUN_ARG

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Infrastructure & Cloud Resources Provisioned Successfully!  <<<${C_RESET}"

load_state

echo -e "${C_YELLOW}${C_BOLD}======================= START COPY&PASTE =======================${C_RESET}"
echo -e "${C_YELLOW}Your Kubernetes Operator and Custom Resources are ready!${C_RESET}"
echo -e "Next steps to run the operator and interact with your bot:\n"

"${SCRIPT_DIR}/print_instructions_gchat.sh" $DRY_RUN_ARG
"${SCRIPT_DIR}/print_instructions_slack.sh" $DRY_RUN_ARG

echo -e "${C_CYAN}${C_BOLD}--- [General Operator & Deployment Next Steps] ---${C_RESET}"
echo -e "[ ] Run the new Operator manager locally or deploy it:"
echo -e "       To run locally: ${C_WHITE}ENABLE_WEBHOOKS=false make run${C_RESET} (from k8s-operator directory)"
echo -e "       To deploy to cluster: ${C_WHITE}make deploy IMG=<your-docker-registry>/kube-agents-operator:latest${C_RESET}"
echo -e ""

echo -e "[ ] Monitor Gateway pod rollout progress:"
echo -e "       ${C_WHITE}kubectl get pods -n ${NAMESPACE:-kubeagents-system}${C_RESET}"
echo -e ""

if [ "$MODEL_PROVIDER" = "chatgpt" ]; then
  get_chatgpt_auth_info
  echo -e ""
  echo -e "[ ] ${C_YELLOW}Complete ChatGPT OAuth Device Flow Authentication:${C_RESET}"
  echo -e "       Because you selected 'chatgpt' as the model provider, LiteLLM must be authenticated"
  echo -e "       via OpenAI's OAuth Device Flow. Please follow these steps to authenticate:"
  if [ -n "$CHATGPT_URL" ] && [ -n "$CHATGPT_CODE" ]; then
    echo -e "       - Open your browser and navigate to: ${C_CYAN}${CHATGPT_URL}${C_RESET}"
    echo -e "       - Enter the code: ${C_CYAN}${CHATGPT_CODE}${C_RESET} and log in to authorize the device."
  else
    echo -e "       - View the LiteLLM gateway logs to check the authentication instructions:"
    echo -e "         ${C_CYAN}kubectl logs -n ${NAMESPACE:-kubeagents-system} deployment/litellm -f${C_RESET}"
    echo -e "       - Follow the instructions displayed in the logs to authorize the device."
  fi
  echo -e "       - Once authorized, the LiteLLM gateway will automatically pair with your ChatGPT subscription."
  echo -e ""
fi

echo -e "======================== END COPY&PASTE ========================\n"
