from huggingface_hub import snapshot_download

print("Starting model download...")

model_path = snapshot_download(
    repo_id="Qwen/Qwen2.5-1.5B-Instruct"
)

print("Download completed!")
print("Model path:", model_path)