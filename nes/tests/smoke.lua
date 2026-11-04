-- FCEUX smoke test: boot, move the cursor, page-scroll, select, try to exit.
-- Saves numbered screenshots to $SHOTS (default: build/shots) and exits the emulator.
-- Run via `make test`.

local out = os.getenv("SHOTS") or "build/shots"

local function wait(n)
  for _ = 1, n do
    emu.frameadvance()
  end
end

local function press(button, frames)
  for _ = 1, (frames or 2) do
    joypad.set(1, { [button] = true })
    emu.frameadvance()
  end
  wait(4) -- release, and let the app redraw
end

local function shot(name)
  gui.savescreenshotas(out .. "/" .. name .. ".png")
  emu.frameadvance() -- the screenshot is written on the next frame
end

wait(90)
shot("1-boot")

press("down")
press("down")
press("down")
shot("2-cursor-down3")

press("down", 80) -- held: key repeat, then a page scroll past entry 16
shot("3-held-down")

press("A")
shot("4-selected")

press("B") -- no EverDrive in the emulator: expect the "can't exit" status
shot("5-b-no-cart")

wait(2)
emu.exit()
