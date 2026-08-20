#include <linux/module.h>
#include <linux/export-internal.h>
#include <linux/compiler.h>

MODULE_INFO(name, KBUILD_MODNAME);

__visible struct module __this_module
__section(".gnu.linkonce.this_module") = {
	.name = KBUILD_MODNAME,
	.init = init_module,
#ifdef CONFIG_MODULE_UNLOAD
	.exit = cleanup_module,
#endif
	.arch = MODULE_ARCH_INIT,
};



static const struct modversion_info ____versions[]
__used __section("__versions") = {
	{ 0x9858f364, "get_random_u8" },
	{ 0xd36dc10c, "get_random_u32" },
	{ 0x15ba50a6, "jiffies" },
	{ 0x56470118, "__warn_printk" },
	{ 0xf0fdf6cb, "__stack_chk_fail" },
	{ 0xd1b5aa5f, "tcp_plb_check_rehash" },
	{ 0x62b0d7e5, "tcp_plb_update_state" },
	{ 0xb70a0e57, "__tcp_send_ack" },
	{ 0x6e2d579e, "param_ops_int" },
	{ 0xbdfb6dbb, "__fentry__" },
	{ 0x5b8239ca, "__x86_return_thunk" },
	{ 0x8aa3d483, "tcp_register_congestion_control" },
	{ 0xe4f92410, "tcp_unregister_congestion_control" },
	{ 0x222d56ed, "tcp_plb_update_state_upon_rto" },
	{ 0x555def05, "module_layout" },
};

MODULE_INFO(depends, "");


MODULE_INFO(srcversion, "2DBBB21BB6DE5933E65E171");
