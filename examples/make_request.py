"""Create a Runpod request from local files or presigned HTTPS URLs.

Examples:
  python examples/make_request.py image_to_video --image frame.png --output request.json
  python examples/make_request.py video_to_video --video clip.mp4 --frames 121 --output request.json
  python examples/make_request.py first_last_frame --first-frame first.png --last-frame https://bucket.s3.amazonaws.com/last.png --output request.json
"""
import argparse
import base64
import json
from pathlib import Path


def source(value):
    if value.startswith("https://"):
        return {"url": value}
    return {"base64": base64.b64encode(Path(value).read_bytes()).decode("ascii")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode")
    parser.add_argument("--prompt", default="A cinematic scene with subtle natural motion, stable framing, and soft ambient sound.")
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--frames", type=int, default=121)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--image")
    parser.add_argument("--video")
    parser.add_argument("--audio")
    parser.add_argument("--first-frame")
    parser.add_argument("--last-frame")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    media = {role: source(getattr(args, role)) for role in ("image", "video", "audio", "first_frame", "last_frame") if getattr(args, role)}
    request = {"input": {"mode": args.mode, "prompt": args.prompt, "parameters": {"width": args.width, "height": args.height, "num_frames": args.frames, "fps": args.fps, "seed": args.seed}, "media": media}}
    if args.mode == "text_to_audio":
        request["input"]["parameters"].pop("width")
        request["input"]["parameters"].pop("height")
    args.output.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}; this file contains media or signed URLs, so keep it private.")


if __name__ == "__main__":
    main()
