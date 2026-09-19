FROM vllm/vllm-openai:latest

# Pre-download the quantized model into the image layer cache is optional —
# by default vLLM will pull it from Hugging Face on first start and cache it
# to HF_HOME (mount a RunPod network volume there so you don't re-download
# ~45GB every time the pod restarts).

ENV HF_HOME=/runpod-volume/huggingface
ENV MODEL_NAME=bullpoint/Qwen3-Coder-Next-AWQ-4bit

COPY serve.sh /serve.sh
RUN chmod +x /serve.sh

ENTRYPOINT ["/serve.sh"]
