import cv2
import numpy as np
#import matplotlib.pyplot as plt
import time


# image = cv2.imread('images/2.png', cv2.IMREAD_GRAYSCALE)
# image[image == 255] = 0
# image[image!=0] = 1
# plt.imshow(image, cmap='gray')
# plt.show()

class CircleDetector:
    def __init__(self, image_path, kernel_size=11, threshold=0.95, start_point=(0, 0), resolution=0.25):
        self.image_path = image_path
        self.image = cv2.imread(self.image_path, cv2.IMREAD_GRAYSCALE)
        self.image[self.image == 255] = 0  # Set all pixel values of 255 to 0
        if kernel_size == 7:
            self.image[self.image == 225] = 0  # Set all pixel values of 255 to 0
        self.image[self.image != 0] = 1  # Set all non-zero pixel values to 1
        self.kernel_size = kernel_size
        self.threshold = threshold
        self.start_point = start_point
        self.resolution = resolution

    def detect_circles(self):
        kernel = np.zeros((self.kernel_size, self.kernel_size), dtype=np.uint8)
        cv2.circle(kernel, (self.kernel_size // 2, self.kernel_size // 2), self.kernel_size // 2, 1, 1)

        plots = []
        for i in range(self.image.shape[0] - self.kernel_size + 1):
            for j in range(self.image.shape[1] - self.kernel_size + 1):
                sub_image = self.image[i:i + self.kernel_size, j:j + self.kernel_size]
                if np.sum(sub_image) < 14: continue
                sim = self.similarity(sub_image, kernel)
                if sim >= self.threshold:
                    plots.append((j + self.kernel_size // 2, i + self.kernel_size // 2))
        plots = [(x * self.resolution + self.start_point[0], self.start_point[1] - y * self.resolution) for x, y in
                 plots]
        return plots

    def similarity(self, sub_image, kernel):
        """
        Calculate the similarity between the sub_image and the kernel.
        """
        return np.float32(np.sum(sub_image * kernel)) / np.float32(np.sum(kernel))

    # def display_circles(self, circles):
    #     """
    #     Display the detected circles on the image.
    #     """
    #     # image_with_circles = np.zeros_like(self.image, dtype=np.uint8)
    #     image = cv2.imread(self.image_path)
    #     for center in circles:
    #         cv2.circle(image, (center[0] - self.start_point[0], center[1] - self.start_point[1]), self.kernel_size // 2,
    #                    (255, 0, 0), 1)
    #     cv2.imwrite('image.jpg', image)
        # plt.imshow(image)
        # plt.axis('off')
        # plt.show()
        # plt.imshow(image)


# if __name__ == "__main__":
#     start = time.time()
#     detector = CircleDetector(image_path='source/1201000_6060000_1202000_6061000.png', kernel_size=7, threshold=1,
#                               start_point=(30, 40))
#     circles = detector.detect_circles()
#     print(circles)
#     print(f"Found {len(circles)} centers")
#     end = time.time()
#     print(f"Time taken: {end - start:.2f} seconds")
#     detector.display_circles(circles)
