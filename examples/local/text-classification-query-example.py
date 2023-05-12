# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
import mii

# gpt2
name = "microsoft/DialogRPT-human-vs-rand"

# roberta
name = "roberta-large-mnli"

print(f"Querying {name}...")

generator = mii.mii_query_handle(f"{name}_deployment")
result = generator.query({'query': "DeepSpeed is the greatest"})
print(result.response)
print("time_taken:", result.time_taken)
