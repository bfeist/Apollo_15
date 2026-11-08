import unittest

import numpy as np
from scipy import signal
from scipy.interpolate import CubicSpline

from research.crosstalk_echo_review import RATE, envelope, subtract, trajectory


class EchoReviewTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1517)
        self.positions = np.arange(10 * RATE)
        self.times = self.positions / RATE - 2
        self.donor = signal.sosfilt(signal.butter(4, [300, 2800], fs=RATE,
                                                  btype='bandpass', output='sos'), rng.normal(size=10*RATE))
        self.spline = CubicSpline(self.positions, self.donor)
        self.calibration = (self.times >= 1) & (self.times < 2)
        self.support = envelope(self.times, .5, 5.5)

    def test_both_echo_directions_with_variable_separation_and_overlap(self):
        # A real shared feed has its own interchannel alignment. The echo is a
        # second path, not simply whichever donor correlation is delayed.
        main = self.spline(np.clip(self.positions + .0173*RATE, 0, len(self.donor)-1))
        rng = np.random.default_rng(87)
        overlap = rng.normal(0, .025, len(main))
        for direction in (-1, 1):
            delay = direction*(1.37 + .001*np.sin(self.times))
            echo = self.spline(np.clip(self.positions + (.0173-delay)*RATE, 0, len(main)-1))
            truth = .7*main + overlap
            target = truth + .12*echo
            fixed, adaptive, report = subtract(target, main, echo, self.calibration, self.support)
            selected = (self.times >= 2.2) & (self.times < 5)
            initial_error = np.linalg.norm((target-truth)[selected])
            self.assertTrue(report['fixed_accepted'])
            for prediction in (fixed, adaptive):
                self.assertLess(np.linalg.norm((target-prediction-truth)[selected]), .2*initial_error)
                np.testing.assert_array_equal(prediction[self.support == 0], 0)

    def test_offset_shared_feed_without_echo_bypasses(self):
        main = self.spline(np.clip(self.positions-.0243*RATE, 0, len(self.donor)-1))
        unrelated_echo = self.spline(np.clip(self.positions+1.347*RATE, 0, len(self.donor)-1))
        for audio in (.7*main, .7*main + np.random.default_rng(9).normal(0,.02,len(main))):
            fixed, adaptive, report = subtract(audio, main, unrelated_echo, self.calibration, self.support)
            self.assertFalse(report['fixed_accepted'])
            np.testing.assert_array_equal(fixed, 0)
            np.testing.assert_array_equal(adaptive, 0)

    def test_fractional_tracking_for_both_polarities(self):
        lag = 10.2 + .5*np.sin(self.times*1.2)
        for polarity in (-1, 1):
            target = polarity*self.spline(np.clip(self.positions+lag,0,len(self.donor)-1))
            aligned, report = trajectory(target,self.donor,10/RATE,4/RATE,polarity=polarity)
            selected=(self.times>0)&(self.times<6)
            self.assertLess(np.std((target-polarity*aligned)[selected])/np.std(target[selected]), .12)
            measured=np.array(report['donor_minus_target_seconds'])*RATE
            expected=10.2+.5*np.sin(np.array(report['time'])*1.2)
            self.assertLess(np.median(abs(measured-expected)), .15)


if __name__ == '__main__':
    unittest.main()
