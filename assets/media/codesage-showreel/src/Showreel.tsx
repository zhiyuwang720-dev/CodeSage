import { AbsoluteFill, staticFile, useCurrentFrame } from "remotion";
import { Audio } from "@remotion/media";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { wipe } from "@remotion/transitions/wipe";
import { Intro } from "./scenes/Intro";
import { Ownership } from "./scenes/Ownership";
import { Investigation } from "./scenes/Investigation";
import { Cross } from "./scenes/Cross";
import { Metrics } from "./scenes/Metrics";
import { C } from "./visual";

export const Showreel = () => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill>
      <TransitionSeries>
        <TransitionSeries.Sequence durationInFrames={102}>
          <Intro />
        </TransitionSeries.Sequence>
        <TransitionSeries.Transition
          presentation={wipe({ direction: "from-right" })}
          timing={linearTiming({ durationInFrames: 12 })}
        />
        <TransitionSeries.Sequence durationInFrames={234}>
          <Ownership />
        </TransitionSeries.Sequence>
        <TransitionSeries.Transition
          presentation={wipe({ direction: "from-right" })}
          timing={linearTiming({ durationInFrames: 12 })}
        />
        <TransitionSeries.Sequence durationInFrames={228}>
          <Investigation />
        </TransitionSeries.Sequence>
        <TransitionSeries.Transition
          presentation={wipe({ direction: "from-right" })}
          timing={linearTiming({ durationInFrames: 12 })}
        />
        <TransitionSeries.Sequence durationInFrames={201}>
          <Cross />
        </TransitionSeries.Sequence>
        <TransitionSeries.Transition
          presentation={wipe({ direction: "from-right" })}
          timing={linearTiming({ durationInFrames: 12 })}
        />
        <TransitionSeries.Sequence durationInFrames={183}>
          <Metrics />
        </TransitionSeries.Sequence>
      </TransitionSeries>
      <Audio src={staticFile("score.wav")} volume={0.8} />
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          width: `${(f / 899) * 100}%`,
          height: 4,
          background: C.green,
        }}
      />
    </AbsoluteFill>
  );
};
