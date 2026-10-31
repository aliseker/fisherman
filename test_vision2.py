import cv2
import numpy as np

img = cv2.imread(r"C:\Users\alise\.gemini\antigravity-ide\brain\8a056ea8-8bdb-4469-9112-2e8d4c87bfc6\.user_uploaded\media_1791260216108.png")
hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

fh, fw = hsv.shape[:2]
valid_area = np.zeros((fh, fw), dtype=np.uint8)
valid_area[35:int(fh * 0.82), 30:fw-30] = 255
valid_area[:int(fh * 0.32), int(fw * 0.60):] = 0

s_channel = hsv[:, :, 1]
_, fish_s_mask = cv2.threshold(s_channel, 60, 255, cv2.THRESH_BINARY_INV)
fish_s_mask = cv2.bitwise_and(fish_s_mask, valid_area)
contours_s, _ = cv2.findContours(fish_s_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
print("Contours with S < 60:", [cv2.contourArea(c) for c in contours_s if cv2.contourArea(c) > 0])

v_channel = hsv[:, :, 2]
_, fish_v_mask = cv2.threshold(v_channel, 50, 255, cv2.THRESH_BINARY_INV)
fish_v_mask = cv2.bitwise_and(fish_v_mask, valid_area)
contours_v, _ = cv2.findContours(fish_v_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
print("Contours with V < 50:", [cv2.contourArea(c) for c in contours_v if cv2.contourArea(c) > 0])
