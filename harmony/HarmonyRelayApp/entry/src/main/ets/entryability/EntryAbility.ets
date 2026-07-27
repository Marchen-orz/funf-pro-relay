/**
 * EntryAbility —— 主入口
 */
import { UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    console.info("[Entry] onCreate");
  }

  onDestroy(): void {
    console.info("[Entry] onDestroy");
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) {
        console.error('Failed to load content: ' + JSON.stringify(err));
        return;
      }
      console.info('Content loaded');
    });
  }

  onWindowStageDestroy(): void {
    console.info("[Entry] onWindowStageDestroy");
  }

  onForeground(): void {
    console.info("[Entry] onForeground");
  }

  onBackground(): void {
    console.info("[Entry] onBackground");
  }
}
