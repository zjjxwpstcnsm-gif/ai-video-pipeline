#!/usr/bin/env python3
import argparse
from ai_video.media.ffmpeg import concat_mp4

p = argparse.ArgumentParser()
p.add_argument("output")
p.add_argument("inputs", nargs="+")
a = p.parse_args()
print(concat_mp4(a.inputs, a.output))
