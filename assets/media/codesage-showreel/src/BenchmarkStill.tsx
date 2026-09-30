import {AbsoluteFill, Img, staticFile} from 'remotion';

export const BenchmarkStill = ({locale}: {locale: 'zh' | 'en'}) => (
  <AbsoluteFill style={{backgroundColor: '#101510'}}>
    <Img src={staticFile(`benchmark-${locale}.svg`)} style={{width: 1600, height: 1000}} />
  </AbsoluteFill>
);
