"""Fill questionnaire markdown answers with randomized values.

This is a test utility for end-to-end flow validation.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import List


def _choose_free_text(question: str) -> str:
	q = question.lower()
	if "first name" in q:
		return random.choice(["Alex", "Jordan", "Taylor", "Morgan", "Riley"])
	if "last name" in q:
		return random.choice(["Nguyen", "Patel", "Garcia", "Kim", "Brown"])
	if "email" in q:
		return "alex.random.test@example.com"
	if "phone" in q:
		return "+12025550123"
	if "linkedin" in q:
		return "https://www.linkedin.com/in/alex-random-test"
	if "website" in q:
		return "https://example.com/portfolio"
	if "location" in q or "city" in q:
		return random.choice(["New York", "Seattle", "Austin", "San Francisco", "Boston"])
	if "how did you hear" in q:
		return "LinkedIn"
	return f"Random answer {random.randint(100, 999)}"


def fill_questionnaire_text(text: str) -> str:
	lines = text.splitlines()
	preamble: List[str] = []
	blocks: List[List[str]] = []
	current: List[str] = []

	for line in lines:
		if line.startswith("## Q"):
			if current:
				blocks.append(current)
			current = [line]
			continue
		if current:
			current.append(line)
		else:
			preamble.append(line)

	if current:
		blocks.append(current)

	output_blocks: List[str] = []
	for lines in blocks:
		question = ""
		options: List[str] = []
		in_options = False
		answer_line = None

		for idx, line in enumerate(lines):
			if line.startswith("Question:"):
				question = line.split(":", 1)[1].strip()
			elif line.startswith("Options:"):
				in_options = True
			elif line.startswith("Answer:"):
				answer_line = idx
				in_options = False
			elif in_options and line.startswith("- "):
				option = line[2:].strip()
				if option and option != "(free text)":
					options.append(option)

		answer = random.choice(options) if options else _choose_free_text(question)
		if answer_line is not None:
			lines[answer_line] = f"Answer: {answer}"

		output_blocks.append("\n".join(lines))

	preamble_text = "\n".join(preamble).rstrip()
	body = "\n\n".join(output_blocks)
	if preamble_text:
		return preamble_text + "\n\n" + body + "\n"
	return body + "\n"


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Fill questionnaire markdown with randomized answers")
	parser.add_argument("--input", required=True, help="Input questionnaire markdown path")
	parser.add_argument("--output", required=True, help="Output filled questionnaire markdown path")
	parser.add_argument("--seed", type=int, default=42, help="Random seed")
	return parser.parse_args()


def main() -> None:
	args = _parse_args()
	random.seed(args.seed)
	src = Path(args.input)
	dst = Path(args.output)
	filled = fill_questionnaire_text(src.read_text(encoding="utf-8"))
	dst.write_text(filled, encoding="utf-8")
	print(f"Saved filled questionnaire to: {dst}")


if __name__ == "__main__":
	main()
