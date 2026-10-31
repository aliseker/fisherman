import cv2
import numpy as np

img = cv2.imread(r"C:\Users\alise\.gemini\antigravity-ide\brain\8a056ea8-8bdb-4469-9112-2e8d4c87bfc6\.user_uploaded\media_1791260216108.png")
hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

red_lower1 = np.array([0, 50, 120])
red_upper1 = np.array([20, 255, 255])
red_lower2 = np.array([160, 50, 120])
red_upper2 = np.array([180, 255, 255])

# Crop to blue water area (exclude borders and time bar)
# In this specific screenshot, the water is roughly from y=50 to y=350, x=50 to x=400
# I will just use the entire image and then crop the edges
fh, fw = hsv.shape[:2]
valid_area = np.zeros((fh, fw), dtype=np.uint8)
valid_area[35:int(fh * 0.82), 30:fw-30] = 255
valid_area[:int(fh * 0.32), int(fw * 0.60):] = 0

mask1 = cv2.inRange(hsv, red_lower1, red_upper1)
mask2 = cv2.inRange(hsv, red_lower2, red_upper2)
red_mask = cv2.bitwise_or(mask1, mask2)
# Crop red mask exactly like thread3 does
safe_h = int(fh * 0.82)
red_mask = red_mask[:safe_h, :]
# Ignore top right 20%
red_mask[:int(fh * 0.20), :] = 0

print("Red pixels:", cv2.countNonZero(red_mask))

v_channel = hsv[:, :, 2]
min_val, _, _, _ = cv2.minMaxLoc(v_channel, mask=valid_area)
print("Min V:", min_val)

mean_val = cv2.mean(v_channel, mask=valid_area)[0]
print("Mean V:", mean_val)

_, fish_mask_10 = cv2.threshold(v_channel, min_val + 10, 255, cv2.THRESH_BINARY_INV)
fish_mask_10 = cv2.bitwise_and(fish_mask_10, valid_area)
contours_10, _ = cv2.findContours(fish_mask_10, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
print("Contours with +10:", [cv2.contourArea(c) for c in contours_10 if cv2.contourArea(c) > 0])

_, fish_mask_20 = cv2.threshold(v_channel, min_val + 20, 255, cv2.THRESH_BINARY_INV)
fish_mask_20 = cv2.bitwise_and(fish_mask_20, valid_area)
contours_20, _ = cv2.findContours(fish_mask_20, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
print("Contours with +20:", [cv2.contourArea(c) for c in contours_20 if cv2.contourArea(c) > 0])

# What about thresholding based on average?
_, fish_mask_mean = cv2.threshold(v_channel, mean_val - 30, 255, cv2.THRESH_BINARY_INV)
fish_mask_mean = cv2.bitwise_and(fish_mask_mean, valid_area)
contours_mean, _ = cv2.findContours(fish_mask_mean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
print("Contours with mean-30:", [cv2.contourArea(c) for c in contours_mean if cv2.contourArea(c) > 0])
