import "./index.css";
import { Composition, Folder, Still } from "remotion";
import { BenchmarkStill } from "./BenchmarkStill";
import { Showreel } from "./Showreel";
import { Intro } from "./scenes/Intro";
import { Ownership } from "./scenes/Ownership";
import { Investigation } from "./scenes/Investigation";
import { Cross } from "./scenes/Cross";
import { Metrics } from "./scenes/Metrics";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="CodeSage"
        component={Showreel}
        durationInFrames={900}
        fps={60}
        width={1920}
        height={1080}
      />
      <Folder name="Benchmark">
        <Still id="BenchmarkZh" component={BenchmarkStill} width={1600} height={1000} defaultProps={{locale: 'zh'}} />
        <Still id="BenchmarkEn" component={BenchmarkStill} width={1600} height={1000} defaultProps={{locale: 'en'}} />
      </Folder>
      <Folder name="Scenes">
        <Composition
          id="Identity"
          component={Intro}
          durationInFrames={102}
          fps={60}
          width={1920}
          height={1080}
        />
        <Composition
          id="Ownership"
          component={Ownership}
          durationInFrames={234}
          fps={60}
          width={1920}
          height={1080}
        />
        <Composition
          id="Investigation"
          component={Investigation}
          durationInFrames={228}
          fps={60}
          width={1920}
          height={1080}
        />
        <Composition
          id="Cross"
          component={Cross}
          durationInFrames={201}
          fps={60}
          width={1920}
          height={1080}
        />
        <Composition
          id="Metrics"
          component={Metrics}
          durationInFrames={183}
          fps={60}
          width={1920}
          height={1080}
        />
      </Folder>
    </>
  );
};
