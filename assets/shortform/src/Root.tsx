import {Composition} from 'remotion';
import {Main} from './Main';
import editData from '../public/edit-data.json';

// Composition size/fps/duration come from edit-data.json — set durationSec to
// the exact cut.mp4 duration (ffprobe) when writing the per-video data.
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Reels"
      component={Main}
      durationInFrames={Math.max(1, Math.round(editData.durationSec * editData.fps))}
      fps={editData.fps}
      width={editData.width}
      height={editData.height}
    />
  );
};
