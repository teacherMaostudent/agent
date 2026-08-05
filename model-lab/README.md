# Model Lab

Model Lab is deliberately separate from the online Agent Runtime. It records
LoRA/QLoRA, DPO/GRPO and distributed-training experiment specifications,
evaluation results and model cards; a successful artifact can then be proposed
to Control Plane for release.

The service does not bundle GPU frameworks into the platform image. Production
workers run a pinned experiment image as a Kubernetes Job or Temporal activity,
write immutable artifacts to object storage, and submit the resulting manifest
back to this API.

## Supported plans

- `lora` / `qlora`: supervised fine-tuning plans;
- `dpo` / `grpo`: preference/alignment plans;
- `distributed`: launcher metadata for DeepSpeed/FSDP/TorchRun workers.

Every plan requires a dataset fingerprint, base-model revision, random seed,
container image digest and evaluation thresholds. A model card is created only
after the submitted evaluation passes those thresholds.
