import {Composition} from 'remotion';
import {Main} from './Main';
import editData from '../public/edit-data.json';

// Composition size/fps/duration come from edit-data.json — MATCH cut.mp4
// exactly (e.g. 1920×1080 @ 30, or 3840×2160 @ 23.976 → use the precise fps).
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Longform"
      component={Main}
      durationInFrames={Math.max(1, Math.round(editData.durationSec * editData.fps))}
      fps={editData.fps}
      width={editData.width}
      height={editData.height}
    />
  );
};
