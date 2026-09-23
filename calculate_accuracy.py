import json
import os
import numpy as np
from scipy.io import wavfile
from phase1_ears_mouth import _transcribe

# --- Config ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(SCRIPT_DIR, "voice_dataset")
MANIFEST_FILE = os.path.join(DATASET_DIR, "manifest.jsonl")

def calculate_accuracy():
    if not os.path.exists(MANIFEST_FILE):
        print(f"Error: Manifest not found at {MANIFEST_FILE}")
        return

    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total_samples = len(lines)
    perfect_matches = 0
    total_words = 0
    correct_words = 0
    failures = []

    print(f"Calculating accuracy over {total_samples} samples...")
    print("-" * 50)

    for i, line in enumerate(lines):
        data = json.loads(line)
        audio_path = os.path.join(DATASET_DIR, data["audio"])
        ground_truth = data["text"].strip().lower().rstrip("?.!")

        try:
            # Load wav file
            sample_rate, audio_data = wavfile.read(audio_path)

            # Ensure audio is 16kHz mono
            if sample_rate != 16000:
                print(f"Warning: {data['audio']} is not 16kHz. Skipping.")
                continue

            if len(audio_data.shape) > 1:
                audio_data = audio_data.mean(axis=1)

            # Convert to the bytes format phase1._transcribe expects (int16 PCM)
            pcm_bytes = audio_data.astype(np.int16).tobytes()

            # Transcribe
            prediction = _transcribe(pcm_bytes)
            if prediction:
                prediction = prediction.strip().lower().rstrip("?.!")
            else:
                prediction = ""

            # Metrics
            if prediction == ground_truth:
                perfect_matches += 1

            # Word-level accuracy
            gt_words = ground_truth.split()
            pred_words = prediction.split()

            total_words += len(gt_words)
            # Simple overlap count for word accuracy
            for word in gt_words:
                if word in pred_words:
                    correct_words += 1

            if prediction != ground_truth:
                failures.append({
                    "file": data["audio"],
                    "expected": ground_truth,
                    "got": prediction
                })

            if (i + 1) % 10 == 0:
                print(f"Processed {i + 1}/{total_samples}...")

        except Exception as e:
            print(f"Error processing {data['audio']}: {e}")

    # Final Report
    perfect_pct = (perfect_matches / total_samples) * 100
    word_accuracy = (correct_words / total_words * 100) if total_words > 0 else 0

    print("\n" + "="*50)
    print("VOICE RECOGNITION ACCURACY REPORT")
    print("="*50)
    print(f"Total Samples:      {total_samples}")
    print(f"Perfect Matches:    {perfect_matches} ({perfect_pct:.2f}%)")
    print(f"Overall Word Acc:   {word_accuracy:.2f}%")
    print("="*50)

    if failures:
        print("\nTop Failures (for debugging):")
        for f in failures[:5]:
            print(f"File: {f['file']}")
            print(f"  Expected: {f['expected']}")
            print(f"  Got:      {f['got']}")
            print("-" * 20)

if __name__ == "__main__":
    calculate_accuracy()
