"""
BETH — Accuracy Test Utility
----------------------------
This script helps measure the current Word Error Rate (WER) of the STT system.
It presents a series of target phrases, records the user saying them,
and compares the transcription to the target.
"""

import os
import sys
from phase1_ears_mouth import listen, speak

# Target phrases to test
TEST_PHRASES = [
    "Open Chrome",
    "Open Spotify",
    "What is the weather like in Bangalore",
    "Search for AI news in my third Chrome profile",
    "Add buy milk to my todo list",
    "Set a timer for ten minutes",
    "Remind me to take the trash out in two hours",
    "Change your voice to British",
    "Find my project notes file",
    "Hey Beth, goodbye",
]

def calculate_accuracy(target, result):
    target = target.lower().strip().rstrip('.').rstrip('?')
    result = result.lower().strip().rstrip('.').rstrip('?')

    if target == result:
        return 1.0

    # Basic word-level accuracy
    t_words = target.split()
    r_words = result.split()

    if not r_words:
        return 0.0

    matches = 0
    for i in range(min(len(t_words), len(r_words))):
        if t_words[i] == r_words[i]:
            matches += 1

    return matches / len(t_words) if t_words else 0.0

def run_test():
    print("=" * 60)
    print("BETH STT ACCURACY TEST")
    print("=" * 60)
    print(f"There are {len(TEST_PHRASES)} phrases to test.")
    print("For each one, Beth will tell you the phrase, then listen for you to repeat it.")
    print("Press Enter to start.")
    input()

    results = []

    for i, target in enumerate(TEST_PHRASES, 1):
        print(f"\nTest {i}/{len(TEST_PHRASES)}")
        print(f"TARGET: {target}")

        speak(f"Please say: {target}")

        # Give the user a moment to start speaking
        text = listen()

        if text is None:
            print("Result: [SILENCE/NO SPEECH]")
            results.append(0.0)
        else:
            print(f"Result: {text}")
            acc = calculate_accuracy(target, text)
            results.append(acc)
            print(f"Accuracy for this phrase: {acc*100:.1f}%")

    total_acc = sum(results) / len(results)

    print("\n" + "=" * 60)
    print(f"FINAL TEST RESULTS")
    print(f"Overall Word Accuracy: {total_acc*100:.2f}%")
    print("=" * 60)

    # Save results to a file for history
    with open("accuracy_results.txt", "a", encoding="utf-8") as f:
        f.write(f"Test run: {total_acc*100:.2f}%\n")

if __name__ == "__main__":
    run_test()
