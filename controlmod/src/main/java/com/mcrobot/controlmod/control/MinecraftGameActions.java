package com.mcrobot.controlmod.control;

import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

import java.util.ArrayList;
import java.util.List;

/** GameActions 的 Forge 客户端实现；必须在主线程调用。 */
public class MinecraftGameActions implements GameActions {
    @Override
    public PlayerState playerState() {
        Minecraft mc = Minecraft.getInstance();
        LocalPlayer p = mc == null ? null : mc.player;
        if (p == null) return null;
        String dim = mc.level == null ? "" : mc.level.dimension().location().toString();
        return new PlayerState(p.getGameProfile().getName(), p.getX(), p.getY(), p.getZ(), dim);
    }

    @Override
    public void sendCommand(String commandWithoutSlash) {
        LocalPlayer p = requirePlayer();
        p.connection.sendCommand(commandWithoutSlash);
    }

    @Override
    public void sendChat(String text) {
        LocalPlayer p = requirePlayer();
        p.connection.sendChat(text);
    }

    @Override
    public void closeScreen() {
        Minecraft mc = Minecraft.getInstance();
        if (mc == null) throw new NotInGameException();
        // 调 onClose 而非直接 setScreen(null)：让 AbstractContainerScreen 顺带关闭容器
        if (mc.screen != null) {
            mc.screen.onClose();
        }
    }

    @Override
    public WorldView.BlockAt blockAt(int x, int y, int z) {
        Minecraft mc = requireMc();
        return readBlock(mc, x, y, z);
    }

    @Override
    public WorldView.Area blocksInBox(int x1, int y1, int z1, int x2, int y2, int z2, int limit) {
        Minecraft mc = requireMc();
        int minX = Math.min(x1, x2), maxX = Math.max(x1, x2);
        int minY = Math.min(y1, y2), maxY = Math.max(y1, y2);
        int minZ = Math.min(z1, z2), maxZ = Math.max(z1, z2);
        long total = (long) (maxX - minX + 1) * (maxY - minY + 1) * (maxZ - minZ + 1);
        int cap = Math.max(0, Math.min(limit, (int) Math.min(total, Integer.MAX_VALUE)));
        List<WorldView.BlockAt> out = new ArrayList<>(cap);
        int seen = 0;
        for (int x = minX; x <= maxX; x++) {
            for (int y = minY; y <= maxY; y++) {
                for (int z = minZ; z <= maxZ; z++) {
                    seen++;
                    if (out.size() < cap) out.add(readBlock(mc, x, y, z));
                }
            }
        }
        return new WorldView.Area(out, seen, seen > out.size());
    }

    @Override
    public WorldView.Hit lookingAt() {
        Minecraft mc = requireMc();
        HitResult hr = mc.hitResult;
        if (hr == null) return WorldView.Hit.none();
        if (hr.getType() == HitResult.Type.BLOCK && hr instanceof BlockHitResult bhr) {
            BlockPos pos = bhr.getBlockPos();
            BlockState st = mc.level.getBlockState(pos);
            String id = BuiltInRegistries.BLOCK.getKey(st.getBlock()).toString();
            double dist = mc.player.position().distanceTo(Vec3.atCenterOf(pos));
            return new WorldView.Hit("block", id, pos.getX(), pos.getY(), pos.getZ(), dist);
        }
        if (hr.getType() == HitResult.Type.ENTITY && hr instanceof EntityHitResult ehr) {
            Entity e = ehr.getEntity();
            String id = BuiltInRegistries.ENTITY_TYPE.getKey(e.getType()).toString();
            int ex = (int) Math.floor(e.getX());
            int ey = (int) Math.floor(e.getY());
            int ez = (int) Math.floor(e.getZ());
            return new WorldView.Hit("entity", id, ex, ey, ez, e.distanceTo(mc.player));
        }
        return WorldView.Hit.none();
    }

    private static WorldView.BlockAt readBlock(Minecraft mc, int x, int y, int z) {
        BlockPos pos = new BlockPos(x, y, z);
        // 未加载区块 getBlockState 会返回 void_air；hasChunkAt 在客户端对远处会误报 true，
        // 所以直接问 ClientChunkCache。验收墙时「没看见」绝不能被当成「是空气」。
        if (!mc.level.getChunkSource().hasChunk(pos.getX() >> 4, pos.getZ() >> 4)) {
            return new WorldView.BlockAt(x, y, z, "", false);
        }
        BlockState st = mc.level.getBlockState(pos);
        String id = BuiltInRegistries.BLOCK.getKey(st.getBlock()).toString();
        return new WorldView.BlockAt(x, y, z, id, true);
    }

    private static Minecraft requireMc() {
        Minecraft mc = Minecraft.getInstance();
        if (mc == null || mc.level == null || mc.player == null) throw new NotInGameException();
        return mc;
    }

    private static LocalPlayer requirePlayer() {
        Minecraft mc = Minecraft.getInstance();
        LocalPlayer p = mc == null ? null : mc.player;
        if (p == null) throw new NotInGameException();
        return p;
    }
}
