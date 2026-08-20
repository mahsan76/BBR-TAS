

this removed the hunk error while patching with below command..

patch tcp_bbr_tas.c < BBR-TAS.patch 


the error of hunk was removed via below command:

python3 fix_hunk1.py tcp_bbr_tas.c

###########################################################################
So the correct sequence for each sweep configuration is:

sudo rmmod tcp_bbr_tas.ko
sudo insmod tcp_bbr_tas.ko tas_pacing_shift_startup=<X> tas_pacing_shift_steady=<Y>